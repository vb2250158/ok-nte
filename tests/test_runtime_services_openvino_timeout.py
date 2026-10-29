import threading
import time
import unittest
from unittest.mock import patch

from src.runtime.services import RuntimeServices


def _call_in_thread(fn, budget):
    result = {}

    def run():
        result["value"] = fn()

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(budget)
    return worker.is_alive(), result.get("value"), time.monotonic() - started


class OpenVINOFutureTimeoutTests(unittest.TestCase):
    """NTE2e-155d: headless -t run never emits start_success, so the
    OpenVINO future never resolved and in_combat blocked forever."""

    def setUp(self):
        patcher = patch.object(RuntimeServices, "_init_openvino", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.services = RuntimeServices()
        self.services.OPENVINO_WAIT_TIMEOUT = 0.3
        self.addCleanup(self.services.stop)

    def test_unresolved_future_does_not_block_openvino_available(self):
        hung, value, elapsed = _call_in_thread(lambda: self.services.openvino_available, 5.0)
        self.assertFalse(hung, "openvino_available blocked on a never-resolving future")
        self.assertFalse(value)
        self.assertLess(elapsed, 2.0)

    def test_negative_result_is_cached_without_repeated_blocking(self):
        hung, value, _ = _call_in_thread(lambda: self.services.openvino_available, 5.0)
        self.assertFalse(hung)
        self.assertFalse(value)
        hung, values, elapsed = _call_in_thread(
            lambda: [self.services.openvino_available for _ in range(50)], 5.0
        )
        self.assertFalse(hung)
        self.assertEqual(values, [False] * 50)
        self.assertLess(elapsed, 0.2)

    def test_late_detector_is_picked_up_after_timeout(self):
        hung, value, _ = _call_in_thread(lambda: self.services.openvino_available, 5.0)
        self.assertFalse(hung)
        self.assertFalse(value)

        class _Detector:
            _openvino_available = True

        self.services._publish_openvino_detector(_Detector())
        self.assertTrue(self.services.openvino_available)

    def test_failed_load_is_unavailable_not_raised(self):
        self.services._publish_openvino_failure(RuntimeError("boom"))
        hung, value, _ = _call_in_thread(lambda: self.services.openvino_available, 5.0)
        self.assertFalse(hung)
        self.assertIs(value, False)

    def test_detect_without_model_returns_empty(self):
        hung, value, _ = _call_in_thread(lambda: self.services.openvino_detect(object()), 5.0)
        self.assertFalse(hung)
        self.assertEqual(value, [])

    def test_first_query_starts_loader_when_start_success_never_fired(self):
        calls = []
        with patch.object(RuntimeServices, "_init_openvino", lambda self: calls.append(1)):
            services = RuntimeServices()
            services.OPENVINO_WAIT_TIMEOUT = 0.3
            self.addCleanup(services.stop)
            hung, _, _ = _call_in_thread(lambda: services.openvino_available, 5.0)
            self.assertFalse(hung)
            deadline = time.monotonic() + 2.0
            while not calls and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
