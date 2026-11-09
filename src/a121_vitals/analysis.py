"""Precompute display signals and replay compatible recordings through Acconeer's processor."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import version
import math
from typing import Any

from acconeer.exptool import a121
from acconeer.exptool.a121.algo import breathing
import numpy as np

FLAGS = ("data_saturated", "frame_delayed", "calibration_needed")
EXPLORATION_TOOL_VERSION = version("acconeer-exptool")


@dataclass
class SignalAnalysis:
    elapsed: np.ndarray
    phase: np.ndarray
    amplitude: np.ndarray
    mean_amplitude: np.ndarray
    breathing: dict[str, Any]
    max_amplitude: float


def load_breathing_settings(record: a121.H5Record) -> tuple[int, breathing.RefAppConfig] | None:
    try:
        settings = record.get_algo_group("breathing")
        return int(settings["ref_app_sensor_id"][()]), breathing.RefAppConfig.from_json(
            settings["ref_app_config"].asstr()[()]
        )
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return None


def prepare_signals(results: a121.StackedResults, shape: tuple[int, ...]) -> SignalAnalysis:
    count = len(results)
    phase = np.empty((count, *shape), dtype=np.float32)
    amplitude = np.empty_like(phase)
    mean_amplitude = np.empty((count, shape[1]), dtype=np.float32)
    block = max(1, 65536 // math.prod(shape))
    for start in range(0, count, block):
        stop = min(start + block, count)
        iq = np.stack([results[index].frame for index in range(start, stop)])
        np.arctan2(iq.imag, iq.real, out=phase[start:stop])
        np.hypot(iq.real, iq.imag, out=amplitude[start:stop])
        mean_amplitude[start:stop] = amplitude[start:stop].mean(axis=1)
    times = results.tick_time
    return SignalAnalysis(times - times[0] if count else times, phase, amplitude, mean_amplitude, {},
                          float(amplitude.max(initial=0)))


def estimate_breathing(
    results: a121.StackedResults,
    sensor: a121.SensorConfig,
    metadata: a121.Metadata,
    elapsed: np.ndarray,
    settings: breathing.RefAppConfig | None,
) -> dict[str, Any]:
    def unavailable(reason: str) -> dict[str, Any]:
        return {"reason": reason, "rates": [], "states": [], "window_s": None}

    if settings is None:
        return unavailable("missing_settings")
    rate = sensor.frame_rate
    config = settings.breathing_config
    duration = config.time_series_length_s
    try:
        supported = (
            rate is not None
            and len(sensor.subsweeps) == 1
            and rate == settings.frame_rate
            and sensor.sweeps_per_frame == settings.sweeps_per_frame
            and 0 < config.lowest_breathing_rate < config.highest_breathing_rate < rate * 30
            and 1 <= duration * rate * sensor.num_points <= 1_000_000
            and 0 < settings.distance_determination_duration <= 120
        )
    except (TypeError, ValueError, OverflowError):
        supported = False
    if not supported or rate is None:
        return unavailable("unsupported_settings")
    if np.any(np.diff(elapsed) <= 0):
        return unavailable("invalid_timing")

    processor_config = breathing.ProcessorConfig()
    processor_config.num_distances_to_analyze = settings.num_distances_to_analyze
    processor_config.distance_determination_duration = settings.distance_determination_duration
    processor_config.use_presence_processor = settings.use_presence_processor
    processor_config.breathing_config = config
    processor_config.presence_config = settings.presence_config
    states_by_name = {
        "NO_PRESENCE_DETECTED": "no_presence",
        "INTRA_PRESENCE_DETECTED": "motion",
        "DETERMINE_DISTANCE_ESTIMATE": "finding_distance",
    }
    rates: list[float | None] = []
    states: list[str] = []
    processor = None
    try:
        for index in range(len(results)):
            result = results[index]
            bad_gap = index > 0 and not 0.5 <= (elapsed[index] - elapsed[index - 1]) * rate <= 1.5
            if bad_gap or any(getattr(result, flag) for flag in FLAGS):
                processor = None
                rates.append(None)
                states.append("invalid_signal")
                continue
            if processor is None:
                processor = breathing.Processor(
                    sensor_config=sensor, processor_config=processor_config, metadata=metadata
                )
            output = processor.process(result)
            value = output.breathing_result.breathing_rate if output.breathing_result else None
            valid = value is not None and math.isfinite(value)
            rates.append(float(value) if valid else None)
            states.append("estimated" if valid else states_by_name.get(output.app_state.name, "warming_up"))
    except (AssertionError, IndexError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return unavailable("unsupported_settings")
    return {"reason": None, "rates": rates, "states": states, "window_s": duration}
