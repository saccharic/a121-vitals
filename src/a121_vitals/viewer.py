"""Bounded, read-only views over Acconeer's official in-memory Record objects."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import math
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from acconeer.exptool import a121
import h5py
import numpy as np

from .analysis import EXPLORATION_TOOL_VERSION, FLAGS, SignalAnalysis, estimate_breathing, load_breathing_settings, prepare_signals
from .recording import summarize_record

MIB = 1024 * 1024
MAX_UPLOAD_BYTES = 32 * MIB
MAX_RECORD_BYTES = 128 * MIB
MAX_CACHE_BYTES = 256 * MIB
MAX_WINDOW_CELLS = 32768
IDLE_SECONDS = 30 * 60


class ViewerError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def check_container(file: h5py.File, limit: int) -> None:
    """Bound HDF5 expansion and reject references outside the uploaded file.

    This is a generic container check. Acconeer alone interprets its schema.
    """
    total = 0
    seen: set[int] = set()
    pending = [file]
    links = 0
    while pending:
        group = pending.pop()
        for name in group:
            links += 1
            if links > 10000:
                raise ViewerError("This file has too many HDF5 objects.", 413)
            link = group.get(name, getlink=True)
            if isinstance(link, h5py.ExternalLink):
                raise ViewerError("Linked HDF5 files are not supported. Upload a self-contained recording.")
            # The official recorder uses internal soft links for legacy paths.
            # Their targets are checked through their actual paths below.
            if isinstance(link, h5py.SoftLink):
                continue
            obj = group[name]
            identity = hash(obj.id)
            if identity in seen:
                continue
            seen.add(identity)
            if isinstance(obj, h5py.Group):
                pending.append(obj)
            else:
                if obj.is_virtual or obj.external:
                    raise ViewerError("External or virtual HDF5 datasets are not supported.")
                total += obj.size * obj.dtype.itemsize
                if total > limit:
                    raise ViewerError("Decoded recording exceeds the viewer's memory limit.", 413)


@dataclass
class Recording:
    id: str
    name: str
    source: str
    record: a121.Record
    summary: dict[str, Any]
    estimated_bytes: int
    last_access: float
    signals: dict[tuple[int, int, int], SignalAnalysis]

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "source": self.source,
                "idle_timeout_s": IDLE_SECONDS, "summary": self.summary}

    def select(self, session: int, group: int, sensor: int, subsweep: int):
        try:
            session_record = self.record.session(session)
            config = session_record.session_config.groups[group][sensor]
            sub = config.subsweeps[subsweep]
            metadata = session_record.extended_metadata[group][sensor]
            results = session_record.extended_stacked_results[group][sensor]
        except (IndexError, KeyError):
            raise ViewerError("Session, group, sensor, or subsweep does not exist.", 404) from None
        offset = int(metadata.subsweep_data_offset[subsweep])
        distances = (sub.start_point + np.arange(sub.num_points) * sub.step_length) * metadata.base_step_length_m
        return results, config, slice(offset, offset + sub.num_points), distances

    def frame(self, session: int, group: int, sensor: int, subsweep: int,
              frame_index: int, sweep_index: int) -> dict[str, Any]:
        results, config, selected, distances = self.select(session, group, sensor, subsweep)
        if frame_index >= len(results) or sweep_index >= config.sweeps_per_frame:
            raise ViewerError("Frame or sweep index is outside this recording.")
        result = results[frame_index]
        # Index the SDK's stacked results BEFORE conversion, so a single-frame
        # request does not decode the entire recording into complex arrays.
        iq = result.frame[sweep_index, selected]
        analysis = self.signals[session, group, sensor]
        return {
            "frame_index": frame_index, "sweep_index": sweep_index,
            "tick": int(result.tick), "tick_time_s": float(result.tick_time),
            "elapsed_s": float(result.tick_time - results[0].tick_time),
            "temperature_c": int(result.temperature),
            "flags": {flag: bool(getattr(result, flag)) for flag in FLAGS},
            "distance_m": distances.tolist(), "i": iq.real.astype(np.int16).tolist(),
            "q": iq.imag.astype(np.int16).tolist(),
            "phase": analysis.phase[frame_index, sweep_index, selected].tolist(),
            "amplitude": analysis.amplitude[frame_index, sweep_index, selected].tolist(),
        }

    def window(self, session: int, group: int, sensor: int, subsweep: int,
               start: int, stop: int, sweep_index: int, max_frames: int) -> dict[str, Any]:
        results, config, selected, distances = self.select(session, group, sensor, subsweep)
        if not (start < stop <= len(results)) or sweep_index >= config.sweeps_per_frame:
            raise ViewerError("Window or sweep index is outside this recording. Stop is exclusive.")
        limit = min(max_frames, MAX_WINDOW_CELLS // len(distances))
        if limit < 1:
            raise ViewerError("This subsweep has too many points for a preview.", 413)
        stride = max(1, math.ceil((stop - start) / limit))
        indices = list(range(start, stop, stride))
        rows = [results[index] for index in indices]
        iq = np.stack([row.frame[sweep_index, selected] for row in rows])
        analysis = self.signals[session, group, sensor]
        return {
            "start": start, "stop": stop, "stride": stride, "frame_indices": indices,
            "sweep_index": sweep_index, "distance_m": distances.tolist(),
            "tick_time_s": [row.tick_time for row in rows], "elapsed_s": analysis.elapsed[indices].tolist(),
            "flags": {flag: [bool(getattr(row, flag)) for row in rows] for flag in FLAGS},
            "i": iq.real.astype(np.int16).tolist(), "q": iq.imag.astype(np.int16).tolist(),
            "phase": analysis.phase[indices, sweep_index, selected].tolist(),
            "amplitude": analysis.amplitude[indices, sweep_index, selected].tolist(),
            "mean_amplitude": analysis.mean_amplitude[indices, selected].tolist(),
        }

    def timeline(self, session: int, group: int, sensor: int) -> dict[str, Any]:
        try:
            analysis = self.signals[session, group, sensor]
        except KeyError:
            raise ViewerError("Session, group, or sensor does not exist.", 404) from None
        return {"elapsed_s": analysis.elapsed.tolist(), "breathing": analysis.breathing,
                "exploration_tool_version": EXPLORATION_TOOL_VERSION,
                "max_amplitude": analysis.max_amplitude}


class RecordingStore:
    def __init__(self, *, record_limit: int = MAX_RECORD_BYTES,
                 cache_limit: int = MAX_CACHE_BYTES, max_records: int = 4,
                 idle_seconds: float = IDLE_SECONDS):
        self.record_limit = record_limit
        self.cache_limit = cache_limit
        self.max_records = max_records
        self.idle_seconds = idle_seconds
        self.records: dict[str, Recording] = {}
        self.lock = threading.RLock()

    def prune(self) -> None:
        with self.lock:
            now = time.monotonic()
            for key in list(self.records):
                if now - self.records[key].last_access >= self.idle_seconds:
                    del self.records[key]

    def add(self, path: Path, name: str, source: str = "upload",
            progress: Callable[[str], None] | None = None) -> Recording:
        with self.lock:
            self.prune()
            if source == "sample":
                for item in self.records.values():
                    if item.source == "sample":
                        item.last_access = time.monotonic()
                        return item
            if len(self.records) >= self.max_records:
                raise ViewerError("Viewer is full. Remove a recording or wait for it to expire.", 503)
            try:
                with h5py.File(path, "r") as file:
                    check_container(file, self.record_limit)
                    with a121.open_record(file) as persistent:
                        estimated = 0
                        for i in range(persistent.num_sessions):
                            session = persistent.session(i)
                            for metadatas in session.extended_metadata:
                                for metadata in metadatas.values():
                                    sweeps, points = metadata.frame_shape
                                    estimated += session.num_frames * (sweeps * points * 24 + points * 4 + 72)
                        if estimated > self.record_limit:
                            raise ViewerError("Recording is too large when decoded (128 MiB limit).", 413)
                        if estimated + sum(r.estimated_bytes for r in self.records.values()) > self.cache_limit:
                            raise ViewerError("Viewer memory is full. Remove a recording and try again.", 503)
                        breathing_settings = load_breathing_settings(persistent)
                        record = a121.InMemoryRecord.from_record(persistent)
                        summary = summarize_record(record)
                        if not any(stream["num_frames"] for s in summary["sessions"] for stream in s["streams"]):
                            raise ViewerError("This recording contains no frames.")
                        # Validate JSON before adding an unusable recording to the cache.
                        json.dumps(summary, allow_nan=False)
                if progress:
                    progress("signals")
                streams = {}
                signals = {}
                for session_index in range(record.num_sessions):
                    session = record.session(session_index)
                    for group, results_by_sensor in enumerate(session.extended_stacked_results):
                        for sensor, results in results_by_sensor.items():
                            metadata = session.extended_metadata[group][sensor]
                            key = (session_index, group, sensor)
                            streams[key] = (results, session.session_config.groups[group][sensor], metadata)
                            signals[key] = prepare_signals(results, metadata.frame_shape)
                if progress:
                    progress("breathing")
                for key, (results, sensor_config, metadata) in streams.items():
                    settings = breathing_settings[1] if breathing_settings and key[2] == breathing_settings[0] else None
                    analysis = signals[key]
                    analysis.breathing = estimate_breathing(results, sensor_config, metadata, analysis.elapsed, settings)
            except ViewerError:
                raise
            except (OSError, a121.RecordError, KeyError, ValueError, TypeError,
                    IndexError, ZeroDivisionError, OverflowError) as exc:
                raise ViewerError("Cannot read this file as an Acconeer A121 recording.") from exc
            item = Recording(uuid.uuid4().hex, Path(name).name[:200], source, record,
                             summary, estimated, time.monotonic(), signals)
            self.records[item.id] = item
            return item

    def get(self, key: str) -> Recording:
        with self.lock:
            self.prune()
            if key not in self.records:
                raise ViewerError("Recording expired or was removed. Please upload it again.", 404)
            item = self.records[key]
            item.last_access = time.monotonic()
            return item

    def remove(self, key: str) -> None:
        with self.lock:
            if self.get(key).source != "sample":
                del self.records[key]
