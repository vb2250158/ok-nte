"""Background services started after the application runtime becomes ready."""

import threading
from concurrent.futures import CancelledError, Future, InvalidStateError
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event

from ok import Logger, get_path_relative_to_exe

logger = Logger.get_logger(__name__)


class RuntimeServices:
    """Own startup and shutdown for the sound and OpenVINO runtime services."""

    # Bounded first wait for the detector. Callers poll every frame, so after
    # this one wait the model is only picked up once ready (never blocks again).
    OPENVINO_WAIT_TIMEOUT = 3.0
    # Pre-teleport/walk prewarm may wait longer so compile never starts mid-hunt.
    OPENVINO_PREWARM_TIMEOUT = 30.0

    def __init__(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("RuntimeServices must be constructed on the main thread")
        # comtypes initializes its importing thread, but its atexit cleanup runs
        # on the main thread. Load it here before any audio worker can import it.
        import comtypes  # noqa: F401

        self._stop_event = Event()
        self._started = False
        self._openvino_model_async = None
        self._openvino_model_future = Future()
        self._openvino_lock = threading.Lock()
        self._openvino_init_started = False
        self._openvino_wait_exhausted = False

    def start(self) -> None:
        if self._started or self._stop_event.is_set():
            return
        self._started = True
        threading.Thread(
            target=self._init_sound_context,
            daemon=True,
            name="SoundContextInit",
        ).start()
        self._ensure_openvino_init()

    def _ensure_openvino_init(self) -> None:
        # Headless `-t` runs never emit start_success, so start() never runs;
        # the first detector query must start the loader itself.
        with self._openvino_lock:
            if self._openvino_init_started or self._stop_event.is_set():
                return
            self._openvino_init_started = True
        threading.Thread(
            target=self._init_openvino,
            daemon=True,
            name="OpenVINOInit",
        ).start()

    def stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        self._openvino_model_future.cancel()
        threading.Thread(
            target=self._shutdown_sound_context,
            daemon=True,
            name="SoundContextShutdown",
        ).start()

    @property
    def openvino_model_async(self):
        if self._openvino_model_async is not None:
            return self._openvino_model_async
        self._ensure_openvino_init()
        timeout = 0 if self._openvino_wait_exhausted else self.OPENVINO_WAIT_TIMEOUT
        try:
            return self._openvino_model_future.result(timeout=timeout)
        except FutureTimeoutError:
            if not self._openvino_wait_exhausted:
                self._openvino_wait_exhausted = True
                logger.warning(
                    f"OpenVINO detector not ready after {timeout:.1f}s; "
                    "treating it as unavailable until it loads"
                )
            return None
        except (CancelledError, Exception) as error:
            if not self._openvino_wait_exhausted:
                self._openvino_wait_exhausted = True
                logger.warning(f"OpenVINO detector unavailable: {error!r}")
            return None

    @property
    def openvino_latency_async(self):
        model = self._openvino_model_async
        return model.latency if model is not None else 0

    @property
    def openvino_latest_image(self):
        return self._openvino_model_async.latest_image if self._openvino_model_async else None

    @property
    def openvino_available(self):
        model = self.openvino_model_async
        return bool(model is not None and model._openvino_available)

    def openvino_detect(
        self, image, sync=False, box=None, threshold=0.5, force=False, mask_regions=None
    ):
        model = self.openvino_model_async
        if model is None:
            return []
        if not sync:
            return model.detect(
                image,
                box=box,
                threshold=threshold,
                label="target",
                force=force,
                mask_regions=mask_regions,
            )
        return model.detect_sync(
            image,
            box=box,
            threshold=threshold,
            label="target",
            mask_regions=mask_regions,
        )

    def openvino_clear_cache(self) -> None:
        model = self.openvino_model_async
        if model is not None:
            model.clear_cache()

    def ensure_openvino_ready(self, timeout: float | None = None) -> bool:
        """Start loader if needed and wait until ready (or timeout).

        Used before teleport/walk so GPU/CPU compile cannot race the hunt.
        """
        if self._openvino_model_async is not None:
            return bool(getattr(self._openvino_model_async, "_openvino_available", False))
        self._ensure_openvino_init()
        wait = self.OPENVINO_PREWARM_TIMEOUT if timeout is None else float(timeout)
        try:
            model = self._openvino_model_future.result(timeout=wait)
        except FutureTimeoutError:
            logger.warning(
                f"OpenVINO prewarm not ready after {wait:.1f}s; continuing without detector"
            )
            return False
        except (CancelledError, Exception) as error:
            logger.warning(f"OpenVINO prewarm unavailable: {error!r}")
            return False
        ready = bool(model is not None and getattr(model, "_openvino_available", False))
        device = getattr(model, "device", None) if model is not None else None
        logger.info(f"OpenVINO prewarm ready={ready} device={device}")
        return ready

    def _init_sound_context(self) -> None:
        from src.sound_trigger.SoundCombatContext import SoundCombatContext

        context = SoundCombatContext()
        if self._stop_event.is_set():
            return
        context.setup(
            task=None,
            sample_path=get_path_relative_to_exe("assets", "sounds", "dodge.wav"),
            counter_attack_sample_path=get_path_relative_to_exe("assets", "sounds", "counter.wav"),
        )
        if self._stop_event.is_set():
            context.shutdown()
            return
        if context.enter() and not self._stop_event.is_set():
            logger.info("SoundCombatContext initialized globally")
        else:
            context.shutdown()

    def _init_openvino(self) -> None:
        if self._stop_event.is_set():
            return
        try:
            logger.info("openvino_model_async Using YOLO26OpenVINOAsyncDetector")
            from src.vision.openvino_detector import YOLO26OpenVINOAsyncDetector

            detector = YOLO26OpenVINOAsyncDetector(
                xml_path=get_path_relative_to_exe("assets", "openvino", "best.xml")
            )
        except BaseException as error:
            self._publish_openvino_failure(error)
        else:
            self._publish_openvino_detector(detector)

    def _shutdown_sound_context(self) -> None:
        try:
            from src.sound_trigger.SoundCombatContext import SoundCombatContext

            SoundCombatContext().shutdown()
        except Exception:
            logger.exception("Failed to shut down SoundCombatContext")

    def _publish_openvino_failure(self, error: BaseException) -> None:
        if self._stop_event.is_set():
            return
        try:
            self._openvino_model_future.set_exception(error)
        except InvalidStateError:
            return
        logger.error(f"OpenVINO detector initialization failed: {error}")

    def _publish_openvino_detector(self, detector) -> None:
        if self._stop_event.is_set():
            return
        try:
            self._openvino_model_future.set_result(detector)
        except InvalidStateError:
            return
        self._openvino_model_async = detector
