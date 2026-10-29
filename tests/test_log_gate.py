import unittest

from src.utils.log_gate import LogGate, LogGateMixin


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Logger:
    def __init__(self):
        self.calls = []

    def info(self, message):
        self.calls.append(("info", message))

    def debug(self, message):
        self.calls.append(("debug", message))

    def warning(self, message):
        self.calls.append(("warning", message))

    def error(self, message, exception=None):
        self.calls.append(("error", message, exception))


class LogGateTests(unittest.TestCase):
    def test_allow_throttles_by_interval(self):
        clock = _Clock()
        gate = LogGate(time_func=clock)

        self.assertTrue(gate.allow("same", 1.0))
        self.assertFalse(gate.allow("same", 1.0))

        clock.advance(1.0)

        self.assertTrue(gate.allow("same", 1.0))

    def test_changed_message_logs_immediately(self):
        clock = _Clock()
        gate = LogGate(time_func=clock)

        self.assertTrue(gate.allow_message("state", "loading", 10.0, changed=True))
        self.assertFalse(gate.allow_message("state", "loading", 10.0, changed=True))
        self.assertTrue(gate.allow_message("state", "ready", 10.0, changed=True))

    def test_unchanged_message_uses_interval_heartbeat(self):
        clock = _Clock()
        gate = LogGate(time_func=clock)

        self.assertTrue(gate.allow_message("state", "loading", 10.0, changed=True))
        self.assertFalse(gate.allow_message("state", "loading", 10.0, changed=True))

        clock.advance(10.0)

        self.assertTrue(gate.allow_message("state", "loading", 10.0, changed=True))

    def test_changed_message_without_interval_only_logs_changes(self):
        gate = LogGate(time_func=_Clock())

        self.assertTrue(gate.allow_message("state", "loading", None, changed=True))
        self.assertFalse(gate.allow_message("state", "loading", None, changed=True))
        self.assertTrue(gate.allow_message("state", "ready", None, changed=True))

    def test_implicit_callsite_key_isolates_call_sites(self):
        clock = _Clock()
        logger = _Logger()
        gate = LogGate(logger, time_func=clock)

        def emit_a():
            return gate.info("from a", interval=10.0)

        def emit_b():
            return gate.info("from b", interval=10.0)

        self.assertTrue(emit_a())
        self.assertFalse(emit_a())
        self.assertTrue(emit_b())
        self.assertEqual(logger.calls, [("info", "from a"), ("info", "from b")])

    def test_error_log_passes_exception(self):
        logger = _Logger()
        gate = LogGate(logger, time_func=_Clock())
        error = RuntimeError("boom")

        self.assertTrue(gate.error("failed", interval=1.0, exception=error))

        self.assertEqual(logger.calls, [("error", "failed", error)])




class _TaskLogger(LogGateMixin):
    def __init__(self):
        super().__init__()
        self.calls = []

    def log_info(self, message, notify=False):
        self.calls.append(("info", message, notify))

    def log_debug(self, message, notify=False):
        self.calls.append(("debug", message, notify))

    def log_warning(self, message, notify=False):
        self.calls.append(("warning", message, notify))

    def log_error(self, message, exception=None, notify=False):
        self.calls.append(("error", message, exception, notify))


class LogGateMixinKeyTests(unittest.TestCase):
    def test_log_info_gated_accepts_explicit_key(self):
        """Regression: wait_in_team_after_teleport passes key=; mixin must forward it."""
        task = _TaskLogger()
        # Would raise TypeError before fix: unexpected keyword argument key
        self.assertTrue(
            task.log_info_gated(
                "post-teleport wait_in_team elapsed=0s/180",
                interval=15,
                key="post_teleport_wait",
            )
        )
        self.assertFalse(
            task.log_info_gated(
                "post-teleport wait_in_team elapsed=1s/180",
                interval=15,
                key="post_teleport_wait",
            )
        )
        self.assertEqual(len(task.calls), 1)
        self.assertEqual(task.calls[0][0], "info")

    def test_explicit_key_shared_across_call_sites(self):
        task = _TaskLogger()
        self.assertTrue(task.log_info_gated("a", interval=10, key="shared"))
        # Different Python callsite but same explicit key -> still throttled
        self.assertFalse(task.log_info_gated("b", interval=10, key="shared"))
        self.assertEqual(len(task.calls), 1)


if __name__ == "__main__":
    unittest.main()
