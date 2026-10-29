import subprocess
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
            POST_TELEPORT_FRESH_SECONDS = 0.0  # model hang inside is_in_team

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
        """hang1003/hang1030: helper wedged in is_in_team; OS WD fail-closes.

        Caller monitors trip marker from OS child; on_trip -> exit_event.
        Soft path: responsive caller disarms within grace (no self-kill).
        """
        import threading
        from src.utils.log_gate import LogGateMixin

        class _Task(LogGateMixin):
            POST_TELEPORT_TEAM_TIMEOUT = 180
            POST_TELEPORT_FRESH_SECONDS = 0.0  # model hang inside is_in_team

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
                # Hold helper like find_one; exit_event lets soft fail-closed end.
                self.hang_entered.set()
                end = time.monotonic() + 8.0
                while time.monotonic() < end:
                    total = 0
                    for i in range(50000):
                        total += i
                    if self.executor.exit_event.is_set():
                        break
                self.hang_finished.set()
                return False

            def wait_until(self, cond, time_out=None, raise_if_not_found=False, settle_time=0.25):
                return cond()

        task = _Task()
        task.wait_in_team_after_teleport = (
            BaseNTETask.wait_in_team_after_teleport.__get__(task, BaseNTETask)
        )
        t0 = time.monotonic()
        with self.assertRaises(CannotFindException):
            task.wait_in_team_after_teleport(time_out=0.5)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.5, f"external WD did not fail-closed in time: {elapsed:.2f}s")
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

        # Marker-only vs test runner: disarm before grace hard-kill.
        wd = arm_external_watchdog(
            0.3,
            "unit_post_teleport",
            on_trip=on_trip,
            kill_on_trip=False,
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not wd.tripped:
            time.sleep(0.05)
        self.assertTrue(wd.tripped)
        self.assertTrue(tripped.is_set())
        wd.close()
        self.assertGreaterEqual(seen.get("elapsed", 0), 0.2)

    def test_os_child_kills_sacrificial_when_not_disarmed(self):
        """hang1030: if parent never disarms, OS child TerminateProcess target."""
        sacrificial = subprocess.Popen(
            [__import__("sys").executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wd = arm_external_watchdog(
                0.4,
                "unit_kill_target",
                target_pid=sacrificial.pid,
                kill_on_trip=True,
                grace_seconds=0.3,
            )
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and sacrificial.poll() is None:
                _ = wd.tripped
                time.sleep(0.05)
            self.assertIsNotNone(
                sacrificial.poll(),
                "OS watchdog must TerminateProcess sacrificial when not disarmed",
            )
            self.assertTrue(wd.tripped)
            # Do not close before observing kill — close after.
            wd.close()
        finally:
            if sacrificial.poll() is None:
                sacrificial.kill()
                sacrificial.wait(timeout=2)


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


def _hunter_task(fresh=0.0, in_team=True):
    """Minimal AnomalyHunter-like task using real BaseNTETask/AnomalyHunter code."""
    from src.tasks.AnomalyHunter import AnomalyHunter
    from src.utils.log_gate import LogGateMixin

    class _Task(LogGateMixin):
        POST_TELEPORT_TEAM_TIMEOUT = 180
        POST_TELEPORT_FRESH_SECONDS = fresh

        def __init__(self):
            super().__init__()
            self.log_info = Mock()
            self.log_warning = Mock()
            self.log_error = Mock()
            self.sleep = Mock()
            self.screenshot = Mock()
            self.send_key = Mock()
            self.send_key_down = Mock()
            self.send_key_up = Mock()
            self.middle_click = Mock()
            self.ensure_main = Mock()
            self.executor = Mock()
            self.executor.connected = Mock(return_value=False)
            self.executor.exit_event = threading.Event()
            self.is_in_team = Mock(return_value=in_team)
            self.in_combat = Mock(return_value=False)
            self.find_interac = Mock(return_value=False)

        def wait_until(self, cond, time_out=None, raise_if_not_found=False, settle_time=0.25, **kw):
            end = time.monotonic() + (time_out or 1)
            while time.monotonic() < end:
                r = cond()
                if r:
                    return r
                time.sleep(0.02)
            return None

        disarm_post_teleport_watchdog = BaseNTETask.disarm_post_teleport_watchdog
        wait_in_team_after_teleport = BaseNTETask.wait_in_team_after_teleport
        walk_until_interac_or_combat = AnomalyHunter.walk_until_interac_or_combat
        _walk_until_interac_or_combat_body = AnomalyHunter._walk_until_interac_or_combat_body
        enter_hunter = AnomalyHunter.enter_hunter
        WALK_METHOD = {}

    return _Task()


def _disarm_logs(task):
    return [str(c.args[0]) for c in task.log_info.call_args_list
            if "disarm reason=" in str(c.args[0])]


class TestPostTeleportPhaseWatchdog(unittest.TestCase):
    """hang1048: WD must span teleport click -> interac/combat confirmed."""

    def test_stale_hud_in_team_rejected_inside_fresh_window(self):
        task = _hunter_task(fresh=10.0)
        with self.assertRaises(CannotFindException):
            task.wait_in_team_after_teleport(time_out=0.5)
        task.is_in_team.assert_not_called()  # before Esc recover (not connected)
        self.assertEqual(_disarm_logs(task), ["fs-stall-post_teleport disarm reason=in_team_timeout"])

    def test_fresh_in_team_after_window_succeeds(self):
        task = _hunter_task(fresh=0.2)
        t0 = time.monotonic()
        self.assertTrue(task.wait_in_team_after_teleport(time_out=3))
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)
        self.assertEqual(_disarm_logs(task), ["fs-stall-post_teleport disarm reason=in_team"])

    def test_phase_kept_armed_then_disarmed_on_combat(self):
        task = _hunter_task()
        self.assertTrue(task.wait_in_team_after_teleport(time_out=5, keep_phase_armed=True))
        wd = task.__dict__["_post_teleport_wd"]
        self.assertFalse(wd._closed)
        self.assertEqual(_disarm_logs(task), [])
        task.in_combat = Mock(side_effect=[False, True])
        self.assertTrue(task.walk_until_interac_or_combat(script=[["w"]], time_out=2))
        self.assertTrue(wd._closed)
        self.assertNotIn("_post_teleport_wd", task.__dict__)
        self.assertEqual(
            _disarm_logs(task),
            ["fs-stall-post_teleport disarm reason=interac_or_combat_confirmed"],
        )

    def test_phase_disarmed_on_exception(self):
        task = _hunter_task()
        task.wait_in_team_after_teleport(time_out=5, keep_phase_armed=True)
        wd = task.__dict__["_post_teleport_wd"]
        task.find_interac = Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            task.enter_hunter("x")
        self.assertTrue(wd._closed)
        self.assertEqual(_disarm_logs(task), ["fs-stall-post_teleport disarm reason=exception"])

    def test_find_one_hang_after_early_in_team_trips_and_terminates(self):
        """hang1048 morphology: in-team OK fast, then find_one wedges in walk."""
        import src.tasks.BaseNTETask as base_mod

        sacrificial = subprocess.Popen(
            [__import__("sys").executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        real_arm = base_mod.arm_external_watchdog

        def arm_on_sacrificial(**kw):
            kw.update(target_pid=sacrificial.pid, kill_on_trip=True, grace_seconds=0.3)
            return real_arm(**kw)

        try:
            task = _hunter_task()
            with patch.object(base_mod, "arm_external_watchdog", side_effect=arm_on_sacrificial):
                self.assertTrue(task.wait_in_team_after_teleport(time_out=1.0, keep_phase_armed=True))
            wd = task.__dict__["_post_teleport_wd"]
            self.assertFalse(wd.tripped)

            def wedged_find_one():
                time.sleep(3.0)  # find_one hang on TaskExecutor (unwatched before)
                return False

            task.find_interac = Mock(side_effect=wedged_find_one)
            task.walk_until_interac_or_combat(script=[["w"]], time_out=0.1)
            self.assertIsNotNone(sacrificial.poll(), "OS WD must terminate target during walk hang")
            self.assertTrue(wd.tripped)
            self.assertTrue(task.executor.exit_event.is_set())
            self.assertTrue(any(("disarm reason=" in m) for m in _disarm_logs(task)))
        finally:
            if sacrificial.poll() is None:
                sacrificial.kill()
                sacrificial.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
