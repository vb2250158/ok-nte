import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.combat.BaseCombatTask import BaseCombatTask, NotInCombatException, TeamSurvivalStatus
from src.patches.featureset_stall_patch import (
    FEATURESET_READ_TIMEOUT_SECONDS,
    FeatureSetStallError,
    _run_with_timeout,
    arm_external_watchdog,
    install_featureset_stall_patch,
    signal_exit_event,
)
from src.tasks.BaseNTETask import BaseNTETask
from src.utils.phase_watchdog import PhaseTimeout, PhaseWatchdog
from ok import CannotFindException


class TestPhaseWatchdog(unittest.TestCase):
    def test_total_timeout_raises(self):
        clock = {"t": 0.0}

        def now():
            return clock["t"]

        watch = PhaseWatchdog("t", total_timeout=5, idle_timeout=None, time_func=now)
        clock["t"] = 4
        watch.check()
        clock["t"] = 5
        with self.assertRaises(PhaseTimeout):
            watch.check()

    def test_idle_timeout_raises_after_touch_gap(self):
        clock = {"t": 0.0}

        def now():
            return clock["t"]

        watch = PhaseWatchdog(
            "idle", total_timeout=100, idle_timeout=3, time_func=now
        )
        clock["t"] = 1
        watch.touch()
        clock["t"] = 3
        watch.check()
        clock["t"] = 4.5
        with self.assertRaises(PhaseTimeout) as ctx:
            watch.check()
        self.assertIn("idle timeout", str(ctx.exception))


class TestFeatureSetStallHelper(unittest.TestCase):
    def test_run_with_timeout_raises_on_stall(self):
        def hang():
            time.sleep(2.0)
            return "done"

        with self.assertRaises(FeatureSetStallError):
            _run_with_timeout(hang, timeout=0.2, label="unit")

    def test_run_with_timeout_returns_value(self):
        self.assertEqual(_run_with_timeout(lambda: 42, timeout=1.0, label="ok"), 42)

    def test_install_is_idempotent(self):
        install_featureset_stall_patch()
        install_featureset_stall_patch()
        import ok.feature.FeatureSet as fs

        self.assertTrue(callable(fs.read_from_json))
        self.assertGreaterEqual(FEATURESET_READ_TIMEOUT_SECONDS, 120)


class TestWaitInTeamAfterTeleport(unittest.TestCase):
    def _task(self, connected=False):
        task = Mock()
        task.POST_TELEPORT_TEAM_TIMEOUT = 180
        task.log_info = Mock()
        task.log_warning = Mock()
        task.log_error = Mock()
        task.log_info_gated = Mock()
        task.sleep = Mock()
        task.screenshot = Mock()
        task.send_key = Mock()
        task.ensure_main = Mock()
        task.is_in_team = Mock(return_value=False)
        task.wait_until = Mock(return_value=None)
        task.executor = Mock()
        task.executor.connected = Mock(return_value=connected)
        task.wait_in_team_after_teleport = (
            BaseNTETask.wait_in_team_after_teleport.__get__(task, BaseNTETask)
        )
        return task

    def test_fail_closed_without_hwnd_skips_esc(self):
        task = self._task(connected=False)
        with self.assertRaises(CannotFindException):
            task.wait_in_team_after_teleport(time_out=1)
        task.screenshot.assert_called_with("post_teleport_stall")
        task.send_key.assert_not_called()
        task.log_warning.assert_any_call(
            "post-teleport recover skipped: executor not connected / no window handle"
        )

    def test_esc_recover_when_connected(self):
        task = self._task(connected=True)
        task.is_in_team = Mock(side_effect=[True])  # after esc
        result = task.wait_in_team_after_teleport(time_out=1)
        self.assertTrue(result)
        task.send_key.assert_called()
        task.screenshot.assert_called_with("post_teleport_stall")



    def test_cond_log_info_gated_key_does_not_typeerror(self):
        """Live bug: cond() called log_info_gated(..., key=) and TypeError aborted wait."""
        from src.utils.log_gate import LogGateMixin

        class _Task(LogGateMixin):
            POST_TELEPORT_TEAM_TIMEOUT = 180

            def __init__(self):
                super().__init__()
                self.log_info = Mock()
                self.log_warning = Mock()
                self.log_error = Mock()
                self.sleep = Mock()
                self.screenshot = Mock()
                self.send_key = Mock()
                self.ensure_main = Mock()
                self.is_in_team = Mock(return_value=True)
                self.executor = Mock()
                self.executor.connected = Mock(return_value=True)

            def wait_until(self, cond, time_out=None, raise_if_not_found=False, settle_time=0.25):
                # Invoke real cond once so log_info_gated(key=...) runs
                return bool(cond())

        task = _Task()
        task.wait_in_team_after_teleport = (
            BaseNTETask.wait_in_team_after_teleport.__get__(task, BaseNTETask)
        )
        # Must not raise TypeError on unexpected keyword argument key
        result = task.wait_in_team_after_teleport(time_out=1)
        self.assertTrue(result)



    def test_is_in_team_hang_fails_closed_within_budget(self):
        """hang0938: if is_in_team/find_one blocks past budget, wait fails closed.

        wait_condition only checks the 180s budget AFTER condition() returns, so
        a wedged find_one starved the outer timeout. External WD must trip.
        """
        from src.utils.log_gate import LogGateMixin

        class _Task(LogGateMixin):
            POST_TELEPORT_TEAM_TIMEOUT = 180

            def __init__(self):
                super().__init__()
                self.log_info = Mock()
                self.log_warning = Mock()
                self.log_error = Mock()
                self.sleep = Mock()
                self.screenshot = Mock()
                self.send_key = Mock()
                self.ensure_main = Mock()
                self.executor = Mock()
                self.executor.connected = Mock(return_value=False)
                self.executor.exit_event = __import__("threading").Event()

            def is_in_team(self, frame=None):
                # Simulate find_one wedged after read_from_json ok
                time.sleep(5.0)
                return False

            def wait_until(self, cond, time_out=None, raise_if_not_found=False, settle_time=0.25):
                # Real wait_until would invoke cond; call once like production loop
                return cond()

        task = _Task()
        task.wait_in_team_after_teleport = (
            BaseNTETask.wait_in_team_after_teleport.__get__(task, BaseNTETask)
        )
        t0 = time.time()
        with self.assertRaises(CannotFindException):
            task.wait_in_team_after_teleport(time_out=0.4)
        elapsed = time.time() - t0
        # Must fail closed near budget, not sit on the 5s hang
        self.assertLess(elapsed, 2.5)
        task.screenshot.assert_called_with("post_teleport_stall")
        # Stall path logged
        warn_msgs = " ".join(str(c) for c in task.log_warning.call_args_list)
        self.assertIn("stall", warn_msgs.lower())

    def test_caller_thread_native_hang_external_watchdog(self):
        """hang1003: hung stack on caller/wait thread (not GIL-releasing sleep stub).

        Live: TaskExecutor blocked inside find_one after read_from_json OK;
        join-poll on fs-stall helper never monitored that stack. Busy-spin
        holds the waiter like a native block; external monotonic WD must
        fail-closed + exit_event without waiting for the hang to finish.
        """
        import threading
        from src.utils.log_gate import LogGateMixin

        class _Task(LogGateMixin):
            POST_TELEPORT_TEAM_TIMEOUT = 180

            def __init__(self):
                super().__init__()
                self.log_info = Mock()
                self.log_warning = Mock()
                self.log_error = Mock()
                self.sleep = Mock()
                self.screenshot = Mock()
                self.send_key = Mock()
                self.ensure_main = Mock()
                self.executor = Mock()
                self.executor.connected = Mock(return_value=False)
                self.executor.exit_event = threading.Event()
                self.hang_entered = threading.Event()
                self.hang_finished = threading.Event()

            def is_in_team(self, frame=None):
                # Native-like: busy-spin holds this thread (GIL in bursts).
                # time.sleep would release GIL and not match hang1003.
                self.hang_entered.set()
                end = time.monotonic() + 8.0
                while time.monotonic() < end:
                    # CPU-bound slice; do not sleep
                    total = 0
                    for i in range(50000):
                        total += i
                    if self.executor.exit_event.is_set():
                        break
                self.hang_finished.set()
                return False

            def wait_until(self, cond, time_out=None, raise_if_not_found=False, settle_time=0.25):
                # Mimic wait_condition: invoke cond on THIS (waiter) thread and
                # block until it returns — same morphology as live find_one.
                return cond()

        task = _Task()
        task.wait_in_team_after_teleport = (
            BaseNTETask.wait_in_team_after_teleport.__get__(task, BaseNTETask)
        )
        t0 = time.monotonic()
        with self.assertRaises(CannotFindException):
            task.wait_in_team_after_teleport(time_out=0.5)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.5, f"external WD did not fail-closed in time: {elapsed:.2f}s")
        self.assertTrue(task.hang_entered.wait(1.0), "hang never entered")
        self.assertTrue(
            task.executor.exit_event.is_set(),
            "external WD must set exit_event (fail-closed)",
        )
        task.screenshot.assert_called_with("post_teleport_stall")
        warn_msgs = " ".join(str(c) for c in task.log_warning.call_args_list)
        self.assertIn("external watchdog", warn_msgs.lower())


class TestExternalMonotonicWatchdog(unittest.TestCase):
    def test_arm_log_and_trip(self):
        import threading

        tripped = threading.Event()
        seen = {}

        def on_trip(elapsed):
            seen["elapsed"] = elapsed
            tripped.set()

        wd = arm_external_watchdog(0.3, "unit_post_teleport", on_trip=on_trip)
        self.assertTrue(tripped.wait(2.0))
        self.assertTrue(wd.tripped)
        wd.close()
        self.assertGreaterEqual(seen.get("elapsed", 0), 0.2)


class TestCombatOnceWatchdog(unittest.TestCase):

    def test_idle_timeout_raises_before_perform(self):
        task = BaseCombatTask.__new__(BaseCombatTask)
        task.chars = []
        task.info = {}
        task.log_info = Mock()
        task.begin_combat_session = Mock()
        task.combat_end = Mock()
        task.wait_in_team = Mock(return_value=True)
        task.wait_until = Mock(return_value=True)
        task.retarget_turn_policy = Mock()
        task.retarget_turn_policy.return_value.__enter__ = Mock(return_value=None)
        task.retarget_turn_policy.return_value.__exit__ = Mock(return_value=False)

        calls = {"n": 0}

        def in_combat():
            calls["n"] += 1
            return calls["n"] < 5

        task.in_combat = in_combat
        task.get_current_char = Mock()

        clock = {"t": 1000.0}

        def fake_time():
            # advance past idle on second check after first touch gap
            return clock["t"]

        real_perform = Mock()

        def perform_side():
            clock["t"] += 200  # exceed idle 180 after first perform+touch

        char = Mock()
        char.perform = Mock(side_effect=perform_side)
        char.is_dead = False
        task.get_current_char.return_value = char

        with patch("src.combat.BaseCombatTask.time") as tmod:
            tmod.time.side_effect = fake_time
            # PhaseWatchdog uses time.time from phase_watchdog module
            with patch("src.utils.phase_watchdog.time") as ptime:
                ptime.time.side_effect = fake_time
                status = task.combat_once(max_combat_time=1200, wait_combat_time=1)
        # Should exit via NotInCombatException path, not hang
        self.assertIn(status, list(TeamSurvivalStatus))
        task.combat_end.assert_called()


if __name__ == "__main__":
    unittest.main()
