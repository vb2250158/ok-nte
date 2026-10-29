import time
import unittest
from unittest.mock import Mock, patch

from src.combat.BaseCombatTask import BaseCombatTask, NotInCombatException, TeamSurvivalStatus
from src.patches.featureset_stall_patch import (
    FEATURESET_READ_TIMEOUT_SECONDS,
    FeatureSetStallError,
    _run_with_timeout,
    install_featureset_stall_patch,
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
