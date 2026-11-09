"""Describe official Record objects; the analysis engine can use them directly.

This module does not implement an HDF5 parser or replace Acconeer's data model.
Its summaries are for inspection, not inputs to signal-processing algorithms.
"""

from __future__ import annotations

import json
import math
from typing import Any

from acconeer.exptool import a121


def summarize_record(record: a121.Record) -> dict[str, Any]:
    """Inspect every session/group/sensor, without retaining the complete IQ array.

    The caller owns the record and must keep a persistent record open while this
    runs. Reading every frame checks readability and shape; no frame is removed,
    averaged, filtered, or reordered. Timestamp issues are reported, not repaired.
    """
    summary: dict[str, Any] = {
        "recording_uuid": record.uuid,
        "recorded_at": record.timestamp,
        "recording_library_version": record.lib_version,
        "server_info": json.loads(record.server_info.to_json()),
        "num_sessions": record.num_sessions,
        "sessions": [],
    }
    for session_index in range(record.num_sessions):
        session = record.session(session_index)
        config = session.session_config
        metadata_groups = session.extended_metadata
        streams: dict[tuple[int, int], dict[str, Any]] = {}
        previous_times: dict[tuple[int, int], float] = {}

        for group_index, (configs, metadatas) in enumerate(
            zip(config.groups, metadata_groups, strict=True)
        ):
            if configs.keys() != metadatas.keys():
                raise ValueError("Sensor IDs in configuration and metadata do not match")
            for sensor_id, sensor_config in configs.items():
                metadata = metadatas[sensor_id]
                shape = (
                    sensor_config.sweeps_per_frame,
                    sum(s.num_points for s in sensor_config.subsweeps),
                )
                if tuple(metadata.frame_shape) != shape:
                    raise ValueError("Frame dimensions in configuration and metadata do not match")
                subsweeps = []
                for index, (sub, offset, length) in enumerate(
                    zip(
                        sensor_config.subsweeps,
                        metadata.subsweep_data_offset,
                        metadata.subsweep_data_length,
                        strict=True,
                    )
                ):
                    if int(length) != sub.num_points or int(offset) != sum(
                        s.num_points for s in sensor_config.subsweeps[:index]
                    ):
                        raise ValueError("Subsweep layout does not match configuration")
                    subsweeps.append({
                        "index": index,
                        "data_offset": int(offset),
                        "distance_m": [
                            (sub.start_point + i * sub.step_length) * metadata.base_step_length_m
                            for i in range(sub.num_points)
                        ],
                    })
                streams[(group_index, sensor_id)] = {
                    "group_index": group_index,
                    "sensor_id": int(sensor_id),
                    "frame_shape": list(shape),
                    "num_frames": 0,
                    "configured_frame_rate_hz": sensor_config.frame_rate,
                    "configured_sweep_rate_hz": sensor_config.sweep_rate,
                    "metadata": json.loads(metadata.to_json()),
                    "subsweeps": subsweeps,
                    "first_tick_time_s": None,
                    "last_tick_time_s": None,
                    "duration_s": None,
                    "interval_min_s": None,
                    "interval_max_s": None,
                    "non_increasing_intervals": 0,
                    "flag_counts": {
                        "data_saturated": 0,
                        "frame_delayed": 0,
                        "calibration_needed": 0,
                    },
                    "temperature_min_c": None,
                    "temperature_max_c": None,
                    "warnings": [],
                }

        for groups in session.extended_results:
            if len(groups) != len(config.groups):
                raise ValueError("Result groups do not match configuration")
            for group_index, results in enumerate(groups):
                if results.keys() != config.groups[group_index].keys():
                    raise ValueError("Result sensor IDs do not match configuration")
                for sensor_id, result in results.items():
                    key = (group_index, sensor_id)
                    stream = streams[key]
                    if list(result.frame.shape) != stream["frame_shape"]:
                        raise ValueError("Recorded IQ frame shape does not match configuration")
                    time_s = float(result.tick_time)
                    if not math.isfinite(time_s):
                        raise ValueError("Frame time is not finite")
                    if key in previous_times:
                        interval = time_s - previous_times[key]
                        stream["non_increasing_intervals"] += int(interval <= 0)
                        for field, operation in (("interval_min_s", min), ("interval_max_s", max)):
                            old = stream[field]
                            stream[field] = interval if old is None else operation(old, interval)
                    else:
                        stream["first_tick_time_s"] = time_s
                    previous_times[key] = time_s
                    stream["last_tick_time_s"] = time_s
                    stream["num_frames"] += 1
                    for flag in stream["flag_counts"]:
                        stream["flag_counts"][flag] += int(getattr(result, flag))
                    for field, operation in (("temperature_min_c", min), ("temperature_max_c", max)):
                        temperature = int(result.temperature)
                        old = stream[field]
                        stream[field] = temperature if old is None else operation(old, temperature)

        for stream in streams.values():
            if stream["num_frames"] != session.num_frames:
                raise ValueError("Read frame count does not match the recorded frame count")
            if stream["num_frames"] == 0:
                stream["warnings"].append("No frames recorded")
            elif stream["non_increasing_intervals"]:
                stream["warnings"].append("Timestamps are not strictly increasing; no duration inferred")
            else:
                stream["duration_s"] = stream["last_tick_time_s"] - stream["first_tick_time_s"]
            for flag, count in stream["flag_counts"].items():
                if count:
                    stream["warnings"].append(f"{flag}: {count} frame(s)")

        summary["sessions"].append({
            "session_index": session_index,
            "session_config": json.loads(config.to_json()),
            "streams": list(streams.values()),
        })
    return summary
