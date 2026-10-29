import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ok import TaskDisabledException
from src.tasks.AnomalyHunter import AnomalyHunter
from src.tasks.BaseNTETask import BaseNTETask
from src.utils import image_utils as iu
from src.vision.openvino_detector import YOLO26OpenVINOAsyncDetector


class FakeUMat:
    """Minimal cv2.UMat stand-in: not subscriptable, exposes .get()."""

    def __init__(self, array):
        self._array = array

    def get(self):
        return self._array

    def __getitem__(self, item):
        raise TypeError("'cv2.UMat' not subscriptable")


class TestOpenVINODevicePolicy(unittest.TestCase):
    def test_preferred_devices_are_cpu_first(self):
        with patch.object(YOLO26OpenVINOAsyncDetector, "_read_cached_device", return_value=None):
            with patch.dict("os.environ", {"OK_NTE_OPENVINO_DEVICE": ""}, clear=False):
                self.assertEqual(
                    YOLO26OpenVINOAsyncDetector.preferred_devices(),
                    ["CPU"],
                )

    def test_gpu_build_failure_falls_back_to_cpu_and_caches(self):
        detector = YOLO26OpenVINOAsyncDetector.__new__(YOLO26OpenVINOAsyncDetector)
        detector._requested_device = "GPU"
        detector.core = SimpleNamespace()
        calls = []

        def compile_model(model, device, config):
            calls.append(device)
            if device == "GPU":
                raise RuntimeError("Status -11: CL_BUILD_PROGRAM_FAILURE ptxas fatal")
            return f"compiled:{device}"

        detector.core.compile_model = compile_model
        cached = []

        with patch.object(YOLO26OpenVINOAsyncDetector, "_read_cached_device", return_value=None):
            with patch.object(
                YOLO26OpenVINOAsyncDetector,
                "_write_cached_device",
                side_effect=lambda device: cached.append(device),
            ):
                with patch.dict("os.environ", {"OK_NTE_OPENVINO_DEVICE": ""}, clear=False):
                    device = detector._resolve_and_compile(object())

        self.assertEqual(device, "CPU")
        self.assertEqual(calls, ["GPU", "CPU"])
        self.assertEqual(detector._compiled_model, "compiled:CPU")
        self.assertIn("CPU", cached)


class TestWalkCaptureGate(unittest.TestCase):
    def _hunter(self):
        hunter = AnomalyHunter.__new__(AnomalyHunter)
        hunter.log_warning = Mock()
        hunter.log_info = Mock()
        hunter.disarm_post_teleport_watchdog = Mock()
        hunter.find_interac = Mock(return_value=False)
        hunter.in_combat = Mock(return_value=False)
        hunter._walk_until_interac_or_combat_body = Mock(return_value=True)
        return hunter

    def test_walk_gated_when_no_frames_does_not_hold_keys(self):
        hunter = self._hunter()
        hunter.is_game_app_hang = Mock(return_value=False)
        hunter.capture_frame_age_seconds = Mock(return_value=0.1)
        hunter.is_hwnd_present_and_visible = Mock(return_value=True)
        hunter.ensure_capture_ready_for_walk = Mock(return_value=(False, "no_frames"))

        ret = hunter.walk_until_interac_or_combat(script=[["w"]], time_out=10)

        self.assertFalse(ret)
        hunter._walk_until_interac_or_combat_body.assert_not_called()
        hunter.disarm_post_teleport_watchdog.assert_called()
        reason = hunter.disarm_post_teleport_watchdog.call_args.args[0]
        self.assertIn("walk_gate:no_frames", reason)

    def test_walk_raises_on_hang_title(self):
        hunter = self._hunter()
        hunter.is_game_app_hang = Mock(return_value=True)
        hunter.game_window_title = Mock(return_value="Application Hang Detected")

        with self.assertRaises(TaskDisabledException):
            hunter.walk_until_interac_or_combat(script=[["w"]], time_out=10)
        hunter._walk_until_interac_or_combat_body.assert_not_called()


class TestCaptureReadyHelper(unittest.TestCase):
    def test_ensure_capture_ready_fails_closed_without_hwnd(self):
        task = BaseNTETask.__new__(BaseNTETask)
        task.is_game_app_hang = Mock(return_value=False)
        task.is_hwnd_present_and_visible = Mock(return_value=False)
        task.capture_frame_age_seconds = Mock(return_value=None)
        task.sleep = Mock()

        with patch.object(BaseNTETask, "frame", new_callable=PropertyMock, return_value=None):
            ok, reason = task.ensure_capture_ready_for_walk(settle_seconds=0.0, min_frames=1)
        self.assertFalse(ok)
        self.assertEqual(reason, "hwnd_not_visible")


class TestUMatHealthBarPath(unittest.TestCase):
    def test_as_numpy_converts_umat_like(self):
        array = np.zeros((4, 4, 3), dtype=np.uint8)
        converted = iu.as_numpy(FakeUMat(array))
        self.assertIsInstance(converted, np.ndarray)
        np.testing.assert_array_equal(converted, array)

    def test_find_red_health_bar_tolerates_umat_frame(self):
        from src.combat.CombatCheck import CombatCheck

        check = CombatCheck.__new__(CombatCheck)
        umat = FakeUMat(np.zeros((100, 100, 3), dtype=np.uint8))
        check.height_of_screen = lambda v: v
        check.width_of_screen = lambda v: v
        check.box_of_screen = Mock(return_value=SimpleNamespace(x=0, y=0, width=50, height=50))
        check.draw_boxes = Mock()

        with patch.object(CombatCheck, "frame", new_callable=PropertyMock, return_value=umat):
            with patch("src.combat.CombatCheck.find_color_rectangles", return_value=[]) as find_boxes:
                with patch(
                    "src.combat.CombatCheck.iu.filter_by_hsv",
                    side_effect=lambda image, *_a, **_k: image,
                ):
                    self.assertFalse(check._find_red_health_bar(width=10))

        args, _kwargs = find_boxes.call_args
        self.assertIsInstance(args[0], np.ndarray)


if __name__ == "__main__":
    unittest.main()
