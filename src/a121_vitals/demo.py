"""Generate an explicitly synthetic example through an official H5 recording."""

from pathlib import Path

import h5py
import numpy as np
from acconeer.exptool import a121
from acconeer.exptool.a121.algo import breathing


def write_demo(path: Path) -> None:
    """No hardware or personal recording is used. Values are deterministic."""
    config = a121.SensorConfig(
        start_point=120, step_length=6, num_points=48, sweeps_per_frame=16,
        frame_rate=20, sweep_rate=3200,
    )
    settings = breathing.get_sitting_config()
    settings.end_m = 1.005
    settings.use_presence_processor = False
    settings.breathing_config.time_series_length_s = 15.0
    settings.presence_config.inter_detection_threshold = 0.1
    client = a121.Client.open(mock=True)
    try:
        with a121.H5Recorder(path, client) as recorder:
            algo = recorder.require_algo_group("breathing")
            algo.create_dataset("ref_app_sensor_id", data=1)
            algo.create_dataset("ref_app_config", data=settings.to_json())
            client.setup_session(config)
            client.start_session()
            client.get_next()
            client.stop_session()
    finally:
        client.close()

    # The SDK creates the file structure. Replace only this generated example's
    # measurement datasets; user recordings are never opened for writing.
    count = 600
    t = np.arange(count)[:, None, None] / 20
    sweep = np.arange(16)[None, :, None] / 3200
    distance = (120 + 6 * np.arange(48))[None, None, :] * 0.0025
    amplitude = 80 + 2200 * np.exp(-((distance - 0.60) / 0.065) ** 2)
    phase = 0.55 * np.sin(2 * np.pi * 0.3 * (t + sweep)) + 8 * distance
    rng = np.random.default_rng(20260929)
    signal = amplitude * np.exp(1j * phase)
    signal += rng.normal(0, 18, signal.shape) + 1j * rng.normal(0, 18, signal.shape)
    with h5py.File(path, "r+") as file:
        result = file["sessions/session_0/group_0/entry_0/result"]
        frame = np.empty(signal.shape, dtype=result["frame"].dtype)
        frame["real"] = np.rint(signal.real).astype(np.int16)
        frame["imag"] = np.rint(signal.imag).astype(np.int16)
        import json
        ticks_per_second = json.loads(file["server_info"][()])["ticks_per_second"]
        values = {
            "frame": frame,
            "tick": np.rint((1 + np.arange(count) / 20) * ticks_per_second).astype(np.int64),
            "temperature": np.full(count, 25),
            "data_saturated": np.zeros(count, dtype=bool),
            "frame_delayed": np.zeros(count, dtype=bool),
            "calibration_needed": np.zeros(count, dtype=bool),
        }
        for name, value in values.items():
            dataset = result[name]
            dataset.resize(value.shape)
            dataset[:] = value
