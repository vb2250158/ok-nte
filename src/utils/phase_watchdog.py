"""Wall-clock phase watchdog for travel / combat / FeatureSet stalls."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


class PhaseTimeout(Exception):
    """Raised when a watched phase exceeds total or idle budget."""


@dataclass
class PhaseWatchdog:
    """Track progress for a long phase and fail closed on stall.

    Call ``touch()`` on progress; call ``check()`` each loop iteration.
    Background ``arm()`` only notifies — it cannot preempt a native hang;
    pair with the FeatureSet stall patch for read-image hangs.
    """

    name: str
    total_timeout: float
    idle_timeout: float | None = None
    on_heartbeat: Callable[[str], None] | None = None
    heartbeat_interval: float = 15.0
    time_func: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        now = self.time_func()
        self.started_at = now
        self.last_touch = now
        self.last_heartbeat = now
        self._armed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_stall: Callable[[str], None] | None = None
        self.tripped = False
        self.trip_reason: str | None = None

    def touch(self) -> None:
        self.last_touch = self.time_func()

    def elapsed(self) -> float:
        return self.time_func() - self.started_at

    def idle_for(self) -> float:
        return self.time_func() - self.last_touch

    def _reason_if_expired(self) -> str | None:
        elapsed = self.elapsed()
        if self.total_timeout > 0 and elapsed >= self.total_timeout:
            return (
                f"{self.name} total timeout {elapsed:.0f}s "
                f">= {self.total_timeout:.0f}s"
            )
        if self.idle_timeout is not None and self.idle_timeout > 0:
            idle = self.idle_for()
            if idle >= self.idle_timeout:
                return (
                    f"{self.name} idle timeout {idle:.0f}s "
                    f">= {self.idle_timeout:.0f}s (elapsed {elapsed:.0f}s)"
                )
        return None

    def check(self) -> None:
        reason = self._reason_if_expired()
        if reason is None:
            self._maybe_heartbeat()
            return
        self.tripped = True
        self.trip_reason = reason
        raise PhaseTimeout(reason)

    def _maybe_heartbeat(self) -> None:
        if self.on_heartbeat is None or self.heartbeat_interval <= 0:
            return
        now = self.time_func()
        if now - self.last_heartbeat < self.heartbeat_interval:
            return
        self.last_heartbeat = now
        self.on_heartbeat(
            f"{self.name} heartbeat elapsed={self.elapsed():.0f}s "
            f"idle={self.idle_for():.0f}s"
        )

    def arm(self, on_stall: Callable[[str], None], poll_interval: float = 1.0) -> None:
        if self._armed:
            return
        self._on_stall = on_stall
        self._stop.clear()
        self._armed = True

        def _run() -> None:
            while not self._stop.wait(poll_interval):
                reason = self._reason_if_expired()
                if reason is None:
                    continue
                self.tripped = True
                self.trip_reason = reason
                try:
                    if self._on_stall is not None:
                        self._on_stall(reason)
                finally:
                    self._stop.set()
                return

        self._thread = threading.Thread(
            target=_run, name=f"PhaseWatchdog-{self.name}", daemon=True
        )
        self._thread.start()

    def disarm(self) -> None:
        self._stop.set()
        self._armed = False
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
