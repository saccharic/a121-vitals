"""Integration tests using synthetic files written by Acconeer's mock client."""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import h5py
import numpy as np
from acconeer.exptool import a121

from a121_vitals.__main__ import main
from a121_vitals.recording import summarize_record


def write_recording(path, sessions):
    client = a121.Client.open(mock=True)
    recorder = a121.H5Recorder(path, client)
    try:
        for config, frame_count in sessions:
            client.setup_session(config)
            client.start_session()
            for _ in range(frame_count):
                client.get_next()
            client.stop_session()
    finally:
        recorder.close()
        client.close()


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "합성 기록.h5"
        config = a121.SensorConfig(sweeps_per_frame=2, start_point=80, num_points=3, step_length=2)
        write_recording(self.path, [(config, 3)])
        # Replace only synthetic measurements, giving independent expected values.
        with h5py.File(self.path, "r+") as file:
            result = file["sessions/session_0/group_0/entry_0/result"]
            frame = result["frame"][:]
            real = np.arange(18, dtype=np.int16).reshape(3, 2, 3) - 9
            imag = np.full_like(real, -2)
            frame["real"], frame["imag"] = real, imag
            result["frame"][:] = frame
            ticks_per_second = json.loads(file["server_info"][()])["ticks_per_second"]
            result["tick"][:] = np.rint(np.array([1.0, 1.05, 1.11]) * ticks_per_second).astype(np.int64)
            for name in ("data_saturated", "frame_delayed", "calibration_needed"):
                result[name][:] = False
            result["temperature"][:] = [24, 25, 26]
            self.expected_iq = real.astype(float) + 1j * imag

    def test_official_iq_and_summary_preserve_measurements(self):
        original_hash = hashlib.sha256(self.path.read_bytes()).digest()
        with a121.open_record(self.path) as record:
            summary = summarize_record(record)
            np.testing.assert_array_equal(record.session(0).frames, self.expected_iq)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), original_hash)
        stream = summary["sessions"][0]["streams"][0]
        self.assertEqual(stream["num_frames"], 3)
        self.assertEqual(stream["frame_shape"], [2, 3])
        self.assertAlmostEqual(stream["duration_s"], 0.11)
        self.assertAlmostEqual(stream["interval_min_s"], 0.05)
        self.assertAlmostEqual(stream["interval_max_s"], 0.06)
        np.testing.assert_allclose(stream["subsweeps"][0]["distance_m"], [0.2, 0.205, 0.21])
        self.assertEqual(stream["temperature_min_c"], 24)
        self.assertEqual(stream["temperature_max_c"], 26)
        self.assertEqual(stream["warnings"], [])
        # The same inspector accepts the official in-memory Record as well.
        self.assertEqual(summarize_record(a121.load_record(self.path)), summary)

    def test_flags_and_bad_timestamps_are_reported_without_repair(self):
        with h5py.File(self.path, "r+") as file:
            result = file["sessions/session_0/group_0/entry_0/result"]
            result["data_saturated"][:] = [False, True, False]
            result["frame_delayed"][:] = [True, False, False]
            result["calibration_needed"][:] = [False, False, True]
            ticks = result["tick"][:]
            ticks[2] = ticks[0]
            result["tick"][:] = ticks
        with a121.open_record(self.path) as record:
            stream = summarize_record(record)["sessions"][0]["streams"][0]
            np.testing.assert_array_equal(record.session(0).stacked_results.tick, ticks)
        self.assertEqual(stream["num_frames"], 3)
        self.assertEqual(stream["non_increasing_intervals"], 1)
        self.assertIsNone(stream["duration_s"])
        self.assertEqual(list(stream["flag_counts"].values()), [1, 1, 1])
        self.assertEqual(len(stream["warnings"]), 4)

    def test_multiple_sessions_groups_sensors_and_subsweeps(self):
        path = Path(self.directory.name) / "extended.h5"
        simple = a121.SensorConfig(sweeps_per_frame=1, start_point=80, num_points=3)
        two_subsweeps = a121.SensorConfig(sweeps_per_frame=2, subsweeps=[
            a121.SubsweepConfig(start_point=80, num_points=3, step_length=2),
            a121.SubsweepConfig(start_point=160, num_points=2, step_length=4),
        ])
        extended = a121.SessionConfig([{1: simple, 2: two_subsweeps}, {1: two_subsweeps}])
        write_recording(path, [(simple, 1), (extended, 2)])
        with a121.open_record(path) as record:
            summary = summarize_record(record)
        self.assertEqual(summary["num_sessions"], 2)
        streams = summary["sessions"][1]["streams"]
        self.assertEqual([(s["group_index"], s["sensor_id"]) for s in streams], [(0, 1), (0, 2), (1, 1)])
        self.assertEqual(streams[1]["frame_shape"], [2, 5])
        self.assertEqual(streams[1]["num_frames"], 2)
        self.assertEqual(streams[1]["subsweeps"][1]["data_offset"], 3)
        np.testing.assert_allclose(streams[1]["subsweeps"][1]["distance_m"], [0.4, 0.41])

    def test_empty_session(self):
        path = Path(self.directory.name) / "empty.h5"
        write_recording(path, [(a121.SensorConfig(), 0)])
        with a121.open_record(path) as record:
            stream = summarize_record(record)["sessions"][0]["streams"][0]
        self.assertEqual(stream["num_frames"], 0)
        self.assertIsNone(stream["duration_s"])
        self.assertEqual(stream["warnings"], ["No frames recorded"])

    def test_inconsistent_frame_shape_is_rejected(self):
        with h5py.File(self.path, "r+") as file:
            result = file["sessions/session_0/group_0/entry_0/result"]
            frame = result["frame"][:, :, :2]
            del result["frame"]
            result.create_dataset("frame", data=frame)
        with a121.open_record(self.path) as record:
            with self.assertRaisesRegex(ValueError, "frame shape"):
                summarize_record(record)

    def test_cli_json_and_human_output(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["inspect", str(self.path), "--json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["num_sessions"], 1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["inspect", str(self.path)]), 0)
        self.assertIn("[3, 2, 3]", output.getvalue())
        self.assertIn("have not been estimated", output.getvalue())

    def test_cli_rejects_missing_non_hdf5_and_non_a121_files(self):
        text = Path(self.directory.name) / "text.h5"
        text.write_text("not an HDF5 file")
        unrelated = Path(self.directory.name) / "unrelated.h5"
        with h5py.File(unrelated, "w") as file:
            file["generation"] = "a111"
        for path in (self.path.with_name("missing.h5"), text, unrelated):
            with self.subTest(path=path):
                error, output = io.StringIO(), io.StringIO()
                with redirect_stderr(error), redirect_stdout(output):
                    self.assertEqual(main(["inspect", str(path), "--json"]), 2)
                self.assertIn("Cannot inspect recording", error.getvalue())
                self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
