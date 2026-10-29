import unittest
from unittest.mock import Mock, patch

from ok import CannotFindException

from src.Labels import Labels
from src.tasks.BaseNTETask import BaseNTETask
from src.tasks.daily.DailyClaimTask import DailyClaimTask


class TestOpenF1panelHardening(unittest.TestCase):
    def _task(self):
        task = object.__new__(BaseNTETask)
        task.find_one = Mock(return_value=None)
        task.is_in_team = Mock(return_value=False)
        task.send_key = Mock()
        task.wait_panel = Mock(return_value=None)
        task.retry_on_action = Mock(side_effect=lambda action, reset: action())
        task.ensure_main = Mock()
        task.screenshot = Mock()
        task.log_info = Mock()
        task.log_warning = Mock()
        task.log_error = Mock()
        task.sleep = Mock()
        return task

    def test_openF1panel_does_not_wait_without_sending_f1_when_not_in_team(self):
        task = self._task()
        task.is_in_team.return_value = False
        task.find_one.return_value = None
        # retry_on_action returns action() result (None) -> raise
        with self.assertRaises(CannotFindException):
            task.openF1panel()
        task.send_key.assert_not_called()
        task.wait_panel.assert_not_called()
        task.log_warning.assert_any_call(
            "openF1panel: not in team; recover overlays before F1"
        )

    def test_openF1panel_accepts_already_open_panel(self):
        task = self._task()
        existing = object()
        task.find_one.return_value = existing
        task.retry_on_action = Mock(side_effect=lambda action, reset: action())
        result = task.openF1panel()
        self.assertIs(result, existing)
        task.send_key.assert_not_called()
        task.wait_panel.assert_not_called()

    def test_openF1panel_dumps_frame_on_miss_after_key(self):
        task = self._task()
        task.is_in_team.return_value = True
        task.find_one.return_value = None
        task.wait_panel.return_value = None
        task.retry_on_action = Mock(side_effect=lambda action, reset: action())
        with self.assertRaises(CannotFindException):
            task.openF1panel()
        task.send_key.assert_called_once_with("f1", after_sleep=1)
        task.screenshot.assert_any_call("openF1panel_miss")
        task.screenshot.assert_any_call("openF1panel_fail")


class TestDailyClaimLeaveMail(unittest.TestCase):
    def _task(self):
        task = object.__new__(DailyClaimTask)
        task.log_info = Mock()
        task.log_warning = Mock()
        task.log_error = Mock()
        task.open_mail_panel = Mock()
        task.operate_click = Mock()
        task.sleep = Mock()
        task.ensure_main = Mock()
        task.is_in_team = Mock(return_value=True)
        task.open_activity_panel = Mock(return_value=True)
        task.find_one = Mock(return_value=None)
        task.get_activity_reward_box = Mock(return_value=None)
        return task

    def test_claim_mail_ensures_main_and_team_before_return(self):
        task = self._task()
        ok = task.claim_mail()
        self.assertTrue(ok)
        task.ensure_main.assert_called()
        task.is_in_team.assert_called()

    def test_claim_activity_requires_team_before_f1(self):
        task = self._task()
        task.is_in_team.return_value = False
        result = task.claim_activity_rewards()
        self.assertFalse(result)
        task.open_activity_panel.assert_not_called()
        task.log_error.assert_called()




class TestIsMainOverlayRecover(unittest.TestCase):
    def _task(self, logged_in=True):
        task = object.__new__(BaseNTETask)
        task.scene = Mock()
        task.scene.logged_in.return_value = logged_in
        task.scene.set_logged_in = Mock()
        task.is_in_team = Mock(return_value=False)
        task.in_team_and_world = Mock(return_value=False)
        task.handle_monthly_card = Mock(return_value=False)
        task.wait_login = Mock(return_value=True)
        task.send_key = Mock()
        return task

    def test_is_main_logged_in_not_in_team_sends_esc_and_is_not_main(self):
        task = self._task(logged_in=True)
        self.assertFalse(task.is_main(esc=True, in_world=False))
        task.wait_login.assert_not_called()
        task.send_key.assert_called_once_with("esc", action_name="is_main", interval=2)

    def test_is_main_does_not_treat_wait_login_truthy_as_main(self):
        task = self._task(logged_in=False)
        task.wait_login.return_value = True
        self.assertFalse(task.is_main(esc=True, in_world=False))
        task.wait_login.assert_called_once()
        task.send_key.assert_not_called()

    def test_is_main_true_only_when_team_hud_visible(self):
        task = self._task(logged_in=False)
        task.is_in_team.return_value = object()
        self.assertTrue(task.is_main(esc=True, in_world=False))
        task.scene.set_logged_in.assert_called_once()
        task.wait_login.assert_not_called()
        task.send_key.assert_not_called()


class TestOpenF2panelHardening(unittest.TestCase):
    def _task(self):
        task = object.__new__(BaseNTETask)
        task.find_one = Mock(return_value=None)
        task.is_in_team = Mock(return_value=False)
        task.send_key = Mock()
        task.wait_panel = Mock(return_value=None)
        task.retry_on_action = Mock(side_effect=lambda action, reset: action())
        task.ensure_main = Mock()
        task.screenshot = Mock()
        task.log_info = Mock()
        task.log_warning = Mock()
        task.log_error = Mock()
        task.sleep = Mock()
        return task

    def test_openF2panel_does_not_wait_without_sending_f2_when_not_in_team(self):
        task = self._task()
        with self.assertRaises(CannotFindException):
            task.openF2panel()
        task.send_key.assert_not_called()
        task.wait_panel.assert_not_called()
        task.log_warning.assert_any_call(
            "openF2panel: not in team; recover overlays before F2"
        )

if __name__ == "__main__":
    unittest.main()
