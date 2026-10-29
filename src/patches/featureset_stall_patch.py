"""Bound FeatureSet.read_from_json so teleport-after reloads cannot hang forever.

Evidence (NTE2e 2026-10-29 Asia/Hong_Kong):
- hang0825 / first-run: teleport @08:24:47 -> read_from_json @08:25:01 then ~27 min silence
- hang0855: teleport @08:55:17 -> read_from_json @08:55:22 then stall to 09:00 kill;
  WGC NULL COM in .err; Esc recover may have no window handle

Fail the image load within ~3 min so callers can fail-closed with evidence
instead of waiting DailyRoutine outer timeout.
"""

from __future__ import annotations

import threading
import time

from ok.util.logger import Logger

logger = Logger.get_logger(__name__)

FEATURESET_READ_TIMEOUT_SECONDS = 180.0

_PATCH_INSTALLED = False
_orig_read_from_json = None


class FeatureSetStallError(RuntimeError):
    """Feature template reload exceeded the stall budget."""


def _run_with_timeout(func, timeout: float, label: str):
    result = []
    error = []

    def _target():
        try:
            result.append(func())
        except Exception as exc:  # noqa: BLE001 - re-raise on join side
            error.append(exc)

    thread = threading.Thread(target=_target, name=f"fs-stall-{label}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise FeatureSetStallError(
            f"FeatureSet {label} stall timeout after {timeout:.0f}s"
        )
    if error:
        raise error[0]
    return result[0] if result else None


def _bounded_read_from_json(*args, **kwargs):
    logger.info(
        "read_from_json watchdog armed "
        f"timeout={FEATURESET_READ_TIMEOUT_SECONDS:.0f}s"
    )
    started = time.monotonic()
    try:
        result = _run_with_timeout(
            lambda: _orig_read_from_json(*args, **kwargs),
            FEATURESET_READ_TIMEOUT_SECONDS,
            "read_from_json",
        )
    except FeatureSetStallError:
        logger.error(
            "read_from_json watchdog TRIPPED "
            f"after {time.monotonic() - started:.0f}s"
        )
        raise
    elapsed = time.monotonic() - started
    logger.info(f"read_from_json watchdog ok elapsed={elapsed:.1f}s")
    return result


def install_featureset_stall_patch():
    global _PATCH_INSTALLED, _orig_read_from_json
    if _PATCH_INSTALLED:
        return
    import ok.feature.FeatureSet as feature_set_mod

    _orig_read_from_json = feature_set_mod.read_from_json
    feature_set_mod.read_from_json = _bounded_read_from_json
    _PATCH_INSTALLED = True
    logger.info(
        "FeatureSet stall patch installed "
        f"timeout={FEATURESET_READ_TIMEOUT_SECONDS:.0f}s"
    )
