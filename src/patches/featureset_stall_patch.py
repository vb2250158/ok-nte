"""Bound FeatureSet.read_from_json so teleport-after reloads cannot hang forever.

Evidence (NTE2e 2026-10-29 Asia/Hong_Kong):
- hang0825 / first-run: teleport @08:24:47 -> read_from_json @08:25:01 then ~27 min silence
- hang0855: teleport @08:55:17 -> read_from_json @08:55:22 then stall to 09:00 kill;
  WGC NULL COM in .err; Esc recover may have no window handle
- hang0938: is_in_team/find_one wedged after read_from_json OK; wait_condition
  only checked 180s after condition() returned
- hang1003: tip 85cb49d loaded, but hang stayed on TaskExecutor inside find_one
  (not fs-stall-post_teleport helper). join-poll never monitored the hung stack;
  ~398s silence, no fail-closed. Need an EXTERNAL monotonic watchdog that does
  not depend on TaskExecutor returning from find_one.
- hang1030: tip 489c7d5 ARMED in-process threading WD but never TRIPPED under
  live find_one/capture hang (interpreter stall); caller mono fallback also
  silent. OS child process must write trip marker then TerminateProcess parent
  if disarm never arrives.

Fail the image load within ~3 min so callers can fail-closed with evidence
instead of waiting DailyRoutine outer timeout.
"""

from __future__ import annotations

import faulthandler
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

FEATURESET_READ_TIMEOUT_SECONDS = 180.0

# Grace after trip marker so a responsive caller can poll, fail-closed, and
# disarm before the OS child hard-kills the target PID (hang1030).
EXTERNAL_WATCHDOG_KILL_GRACE_SECONDS = 2.0

_PATCH_INSTALLED = False
_orig_read_from_json = None

# Child script: wall-clock sleep outside parent interpreter (hang1030).
# argv: timeout label trip_file target_pid grace disarm_file
_OS_WATCHDOG_CHILD = r"""
import os
import sys
import time

timeout = float(sys.argv[1])
label = sys.argv[2]
trip_file = sys.argv[3]
target_pid = int(sys.argv[4])
grace = float(sys.argv[5])
disarm_file = sys.argv[6]


def disarmed() -> bool:
    return os.path.exists(disarm_file)


def sleep_until(deadline: float, slice_s: float = 0.1) -> bool:
    while True:
        if disarmed():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(slice_s, remaining))


started = time.monotonic()
if sleep_until(started + max(0.0, timeout)):
    sys.exit(0)

elapsed = time.monotonic() - started
try:
    with open(trip_file, "w", encoding="utf-8") as fh:
        fh.write(
            f"TRIPPED label={label} after={elapsed:.1f}s "
            f"target_pid={target_pid}\n"
        )
        fh.flush()
        os.fsync(fh.fileno())
except Exception:
    pass

if sleep_until(time.monotonic() + max(0.0, grace), slice_s=0.05):
    sys.exit(0)

if disarmed():
    sys.exit(0)

if target_pid > 0:
    try:
        if sys.platform == "win32":
            import ctypes

            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_TERMINATE, False, target_pid
            )
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(target_pid, 9)
    except Exception:
        pass
sys.exit(1)
"""


class FeatureSetStallError(RuntimeError):
    """Feature template reload exceeded the stall budget."""


def _terminate_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            import ctypes

            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_TERMINATE, False, int(pid)
            )
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(int(pid), 9)
    except Exception:
        pass


class ExternalMonotonicWatchdog:
    """OS-external fail-closed wall clock (hang1003 / hang1030).

    Spawns a child *process* (not a Python thread) that sleeps on its own
    monotonic clock, writes a TRIPPED marker, then TerminateProcess(target)
    after a short grace if not disarmed. Survives parent GIL/native hangs.
    """

    def __init__(
        self,
        timeout: float,
        label: str,
        on_trip=None,
        poll_interval: float = 0.2,
        target_pid: int | None = None,
        kill_on_trip: bool = True,
        grace_seconds: float | None = None,
    ):
        self.timeout = max(0.0, float(timeout))
        self.label = label
        self.on_trip = on_trip
        self.poll_interval = max(0.05, float(poll_interval))
        self.target_pid = int(os.getpid() if target_pid is None else target_pid)
        self.kill_on_trip = bool(kill_on_trip)
        self.grace_seconds = (
            EXTERNAL_WATCHDOG_KILL_GRACE_SECONDS
            if grace_seconds is None
            else max(0.0, float(grace_seconds))
        )
        self.started_mono = None
        self._tripped_latched = False
        self._on_trip_fired = False
        self._closed = False
        self._proc = None
        self._trip_file = None
        self._disarm_file = None
        self._lock = threading.Lock()
        # Diagnostic-only: all-thread stack dump before OS child kills us.
        self.stack_file = None
        self._stack_fh = None

    @property
    def tripped(self) -> bool:
        self._refresh_trip()
        return self._tripped_latched

    def _refresh_trip(self) -> None:
        if self._tripped_latched:
            return
        path = self._trip_file
        if not path or not os.path.exists(path):
            return
        with self._lock:
            if self._tripped_latched:
                return
            self._tripped_latched = True
            elapsed = 0.0
            if self.started_mono is not None:
                elapsed = time.monotonic() - self.started_mono
            if self.on_trip is not None and not self._on_trip_fired:
                self._on_trip_fired = True
                try:
                    self.on_trip(elapsed)
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        f"fs-stall-{self.label} on_trip failed: {exc}"
                    )

    def arm(self) -> "ExternalMonotonicWatchdog":
        # Unconditional engagement log so live runs prove wrap is active.
        logger.info(
            f"fs-stall-{self.label} external watchdog armed "
            f"timeout={self.timeout:.0f}s"
        )
        self.started_mono = time.monotonic()
        unique = uuid.uuid4().hex[:12]
        base = os.path.join(
            tempfile.gettempdir(),
            f"ok-nte-fs-stall-{self.label}-{self.target_pid}-{unique}",
        )
        self._trip_file = base + ".trip"
        self._disarm_file = base + ".disarm"
        for path in (self._trip_file, self._disarm_file):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

        self._arm_stack_dump(base)

        kill_grace = self.grace_seconds if self.kill_on_trip else 0.0
        # kill_on_trip False: still write marker, but use huge grace and skip
        # terminate by pointing target at pid 0 after marker-only mode.
        target = self.target_pid if self.kill_on_trip else 0
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _OS_WATCHDOG_CHILD,
                f"{self.timeout:.6f}",
                self.label,
                self._trip_file,
                str(target),
                f"{kill_grace:.6f}",
                self._disarm_file,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
        return self

    def _arm_stack_dump(self, base: str) -> None:
        """Diagnostic only: faulthandler dumps all Python thread stacks from
        its own C thread (no GIL needed) shortly before the OS child trips,
        so a frozen TaskExecutor stack survives the hard kill."""
        if self.target_pid != os.getpid() or self.timeout <= 0:
            return
        lead = min(5.0, self.timeout / 2.0)
        delay = max(0.05, self.timeout - lead)
        path = base + f"-{time.strftime('%Y%m%d-%H%M%S')}.stack.txt"
        try:
            fh = open(path, "w", encoding="utf-8")
            fh.write(
                f"ok-nte stack dump label={self.label} pid={self.target_pid} "
                f"timeout={self.timeout:.1f}s dump_after={delay:.1f}s\n"
            )
            fh.flush()
            faulthandler.dump_traceback_later(delay, repeat=False, file=fh, exit=False)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"fs-stall-{self.label} stack dump arm failed: {exc}")
            return
        self._stack_fh = fh
        self.stack_file = path
        logger.info(
            f"fs-stall-{self.label} stack dump armed after={delay:.1f}s "
            f"file={path}"
        )

    def _cancel_stack_dump(self) -> None:
        fh = self._stack_fh
        if fh is None:
            return
        self._stack_fh = None
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._cancel_stack_dump()
        # Disarm child before hard kill window.
        if self._disarm_file:
            try:
                with open(self._disarm_file, "w", encoding="utf-8") as fh:
                    fh.write("disarm\n")
                    fh.flush()
            except OSError:
                pass
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.0)
            except Exception:
                _terminate_pid(proc.pid)
        self._refresh_trip()


def arm_external_watchdog(
    timeout: float,
    label: str,
    on_trip=None,
    poll_interval: float = 0.2,
    target_pid: int | None = None,
    kill_on_trip: bool = True,
    grace_seconds: float | None = None,
) -> ExternalMonotonicWatchdog:
    return ExternalMonotonicWatchdog(
        timeout=timeout,
        label=label,
        on_trip=on_trip,
        poll_interval=poll_interval,
        target_pid=target_pid,
        kill_on_trip=kill_on_trip,
        grace_seconds=grace_seconds,
    ).arm()


def signal_exit_event(owner=None) -> bool:
    """Best-effort set ok exit_event for fail-closed (hang1003)."""
    candidates = []
    if owner is not None:
        ex = getattr(owner, "executor", None)
        if ex is not None:
            candidates.append(getattr(ex, "exit_event", None))
        candidates.append(getattr(owner, "exit_event", None))
    try:
        from ok import og

        candidates.append(getattr(og, "exit_event", None))
        ok_instance = getattr(og, "ok", None) or getattr(og, "ok_instance", None)
        if ok_instance is not None:
            candidates.append(getattr(ok_instance, "exit_event", None))
    except Exception:
        pass
    for ev in candidates:
        if ev is None or not hasattr(ev, "set"):
            continue
        try:
            ev.set()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"exit_event.set failed: {exc}")
    return False


def _run_with_timeout(
    func,
    timeout: float,
    label: str,
    on_heartbeat=None,
    heartbeat_interval: float = 15.0,
    poll_interval: float = 1.0,
):
    """Run `func` on a daemon thread; fail closed if it exceeds `timeout`.

    Polls `join` so optional `on_heartbeat` can keep gated elapsed logs
    alive while the callee (e.g. is_in_team/find_one) is wedged after
    read_from_json already returned OK (hang0938).

    hang1003/hang1030: this alone is insufficient when the hung stack is the
    caller (TaskExecutor) itself or the whole interpreter — use
    ExternalMonotonicWatchdog (OS child) for post-teleport.
    """
    result = []
    error = []

    def _target():
        try:
            result.append(func())
        except Exception as exc:  # noqa: BLE001 - re-raise on join side
            error.append(exc)

    thread = threading.Thread(target=_target, name=f"fs-stall-{label}", daemon=True)
    thread.start()
    started = time.monotonic()
    last_hb = started
    deadline = started + max(0.0, timeout)
    while thread.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FeatureSetStallError(
                f"FeatureSet {label} stall timeout after {timeout:.0f}s"
            )
        thread.join(min(poll_interval, remaining))
        if on_heartbeat is None or heartbeat_interval <= 0:
            continue
        now = time.monotonic()
        if now - last_hb >= heartbeat_interval:
            last_hb = now
            on_heartbeat(time.monotonic() - started)
    if error:
        raise error[0]
    return result[0] if result else None


def _bounded_read_from_json(*args, **kwargs):
    logger.info(
        "read_from_json watchdog armed "
        f"timeout={FEATURESET_READ_TIMEOUT_SECONDS:.0f}s"
    )
    started = time.monotonic()
    try:
        result = _run_with_timeout(
            lambda: _orig_read_from_json(*args, **kwargs),
            FEATURESET_READ_TIMEOUT_SECONDS,
            "read_from_json",
        )
    except FeatureSetStallError:
        logger.error(
            "read_from_json watchdog TRIPPED "
            f"after {time.monotonic() - started:.0f}s"
        )
        raise
    elapsed = time.monotonic() - started
    logger.info(f"read_from_json watchdog ok elapsed={elapsed:.1f}s")
    return result


def install_featureset_stall_patch():
    global _PATCH_INSTALLED, _orig_read_from_json
    if _PATCH_INSTALLED:
        return
    import ok.feature.FeatureSet as feature_set_mod

    _orig_read_from_json = feature_set_mod.read_from_json
    feature_set_mod.read_from_json = _bounded_read_from_json
    _PATCH_INSTALLED = True
    logger.info(
        "FeatureSet stall patch installed "
        f"timeout={FEATURESET_READ_TIMEOUT_SECONDS:.0f}s"
    )
