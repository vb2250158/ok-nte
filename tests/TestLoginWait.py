import ast
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class LoginWaitFailed(Exception):
    pass


def load_login_task():
    source = Path(__file__).resolve().parents[1] / "src/tasks/BaseNTETask.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    task_class = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "BaseNTETask"
    )
    task_class.bases = []
    task_class.body = [
        node
        for node in task_class.body
        if (isinstance(node, ast.FunctionDef) and node.name == "wait_login")
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id in {"LOGIN_CLICK_LIMIT", "LOGIN_WAIT_SECONDS"}
                for target in node.targets
            )
        )
    ]
    namespace = {
        "time": time,
        "Labels": SimpleNamespace(login_setting="login_setting"),
        "WaitFailedException": LoginWaitFailed,
    }
    exec(compile(ast.Module(body=[task_class], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["BaseNTETask"]


class TestLoginWait(unittest.TestCase):
    def setUp(self):
        self.task = load_login_task()()
        self.task._login_wait_started = None
        self.task._login_click_count = 0
        self.task.scene = SimpleNamespace(logged_in=Mock(return_value=False))
        self.task.is_in_team = Mock(return_value=False)
        self.task.find_one = Mock(return_value=True)
        self.task.handle_monthly_card = Mock()
        self.task.is_foreground = Mock(return_value=True)
        for name in ("log_info", "log_error", "bring_to_front", "sleep", "operate_click"):
            setattr(self.task, name, Mock())
        self.clock = patch.object(time, "monotonic", return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_persistent_login_stops_after_five_clicks(self):
        for _ in range(5):
            self.assertFalse(self.task.wait_login())
        with self.assertRaises(LoginWaitFailed):
            self.task.wait_login()
        self.assertEqual(self.task.operate_click.call_count, 5)

    def test_timeout_stops_even_when_login_template_disappears(self):
        self.task.wait_login()
        self.task.find_one.return_value = False
        self.now.return_value = 160
        with self.assertRaises(LoginWaitFailed):
            self.task.wait_login()
        self.assertEqual(self.task.operate_click.call_count, 1)

    def test_flickering_template_does_not_reset_budget(self):
        for _ in range(5):
            self.task.find_one.return_value = False
            self.task.wait_login()
            self.task.find_one.return_value = True
            self.task.wait_login()
        with self.assertRaises(LoginWaitFailed):
            self.task.wait_login()

    def test_success_wins_over_expired_budget_and_resets_it(self):
        for source in (self.task.scene.logged_in, self.task.is_in_team):
            with self.subTest(source=source):
                self.task._login_wait_started = 0
                self.task._login_click_count = 5
                source.return_value = True
                self.assertTrue(self.task.wait_login())
                self.assertIsNone(self.task._login_wait_started)
                self.assertEqual(self.task._login_click_count, 0)
                source.return_value = False
        self.task.operate_click.assert_not_called()

    def test_no_login_screen_does_not_start_budget(self):
        self.task.find_one.return_value = False
        self.task.wait_login()
        self.now.return_value = 1000
        self.task.wait_login()
        self.assertIsNone(self.task._login_wait_started)
        self.task.operate_click.assert_not_called()

    def test_new_attempt_can_start_after_failure(self):
        self.task._login_wait_started = 0
        with self.assertRaises(LoginWaitFailed):
            self.task.wait_login()
        self.assertFalse(self.task.wait_login())
        self.task.operate_click.assert_called_once()

    def test_background_login_preserves_foreground_handoff(self):
        self.task.is_foreground.return_value = False
        self.assertFalse(self.task.wait_login())
        self.task.bring_to_front.assert_called_once()
        self.task.sleep.assert_called_once_with(3)
        self.task.operate_click.assert_called_once_with(0.499, 0.865, after_sleep=3)
