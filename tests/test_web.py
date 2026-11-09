"""Check the HTTP pipeline against independently specified IQ measurements."""

import hashlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acconeer.exptool import a121
from fastapi.testclient import TestClient
import h5py
import numpy as np

from a121_vitals.viewer import RecordingStore
from a121_vitals.web import SAMPLE_PATH, create_app
from test_recording import write_recording


class WebTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.h5"
        config = a121.SensorConfig(sweeps_per_frame=2, subsweeps=[
            a121.SubsweepConfig(start_point=80, num_points=3, step_length=2),
            a121.SubsweepConfig(start_point=160, num_points=2, step_length=4),
        ])
        write_recording(self.path, [(config, 5)])
        self.i = np.arange(50).reshape(5, 2, 5) - 20
        self.i[0, 0, :2] = [-32768, 32767]
        self.q = -self.i // 2
        with h5py.File(self.path, "r+") as file:
            result = file["sessions/session_0/group_0/entry_0/result"]
            data = result["frame"][:]
            data["real"], data["imag"] = self.i, self.q
            result["frame"][:] = data
            ticks_per_second = json.loads(file["server_info"][()])["ticks_per_second"]
            result["tick"][:] = np.rint(np.array([1, 1.05, 1.11, 1.2, 1.21]) * ticks_per_second)
            result["data_saturated"][:] = [False, True, False, False, False]
            result["frame_delayed"][:] = False
            result["calibration_needed"][:] = False
            result["temperature"][:] = 27
        self.store = RecordingStore()
        self.client = self.enterContext(TestClient(create_app(store=self.store)))

    def upload(self):
        response = self.client.post('/api/recordings?filename=test.h5', content=self.path.read_bytes(),
                                    headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_exact_iq_timing_and_flags_through_http(self):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        record = self.upload()
        self.assertEqual(record['source'], 'upload')
        self.assertEqual(record['summary']['sessions'][0]['streams'][0]['flag_counts']['data_saturated'], 1)
        url = f"/api/recordings/{record['id']}/frame"
        value = self.client.get(url, params={'frame_index': 1, 'sweep_index': 1, 'subsweep': 1}).json()
        self.assertEqual(value['i'], self.i[1, 1, 3:].tolist())
        self.assertEqual(value['q'], self.q[1, 1, 3:].tolist())
        np.testing.assert_allclose(value['phase'], np.arctan2(self.q[1, 1, 3:], self.i[1, 1, 3:]))
        np.testing.assert_allclose(value['amplitude'], np.hypot(self.i[1, 1, 3:], self.q[1, 1, 3:]))
        np.testing.assert_allclose(value['distance_m'], [0.4, 0.41])
        self.assertAlmostEqual(value['elapsed_s'], .05)
        self.assertTrue(value['flags']['data_saturated'])
        self.assertEqual(value['temperature_c'], 27)
        raw = self.client.get(url).json()
        self.assertEqual(raw['i'][:2], [-32768, 32767])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)

    def test_preview_selects_frames_without_averaging_or_inventing_times(self):
        record = self.upload()
        url = f"/api/recordings/{record['id']}/window"
        response = self.client.get(url, params={'start': 0, 'stop': 5, 'max_frames': 2, 'sweep_index': 1})
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertEqual(value['stride'], 3)
        self.assertEqual(value['frame_indices'], [0, 3])
        self.assertEqual(value['i'], self.i[[0, 3], 1, :3].tolist())
        self.assertEqual(value['q'], self.q[[0, 3], 1, :3].tolist())
        expected_amplitude = np.hypot(self.i[[0, 3], :, :3], self.q[[0, 3], :, :3])
        np.testing.assert_allclose(value['mean_amplitude'], expected_amplitude.mean(axis=1))
        np.testing.assert_allclose(value['amplitude'], expected_amplitude[:, 1, :])
        np.testing.assert_allclose(value['elapsed_s'], [0, .2])
        value = self.client.get(url, params={'start': 1, 'stop': 3}).json()
        self.assertEqual(value['stride'], 1)
        self.assertEqual(value['flags']['data_saturated'], [True, False])

    def test_complete_timeline_and_unsupported_breathing(self):
        stages = []
        item = self.store.add(self.path, self.path.name, progress=stages.append)
        self.assertEqual(stages, ['signals', 'breathing'])
        response = self.client.get(f'/api/recordings/{item.id}/timeline')
        self.assertEqual(response.status_code, 200)
        timeline = response.json()
        np.testing.assert_allclose(timeline['elapsed_s'], [0, .05, .11, .2, .21])
        self.assertEqual(timeline['breathing']['reason'], 'missing_settings')
        self.assertEqual(timeline['breathing']['rates'], [])

    def test_csv_preserves_original_values_and_subsweep_offsets(self):
        record = self.upload()
        response = self.client.get(f"/api/recordings/{record['id']}/frame.csv", params={
            'subsweep': 1, 'frame_index': 4, 'sweep_index': 1,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response.headers['content-disposition'])
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual([int(row['i']) for row in rows], self.i[4, 1, 3:].tolist())
        self.assertEqual([int(row['q']) for row in rows], self.q[4, 1, 3:].tolist())
        self.assertEqual([int(row['point_in_frame']) for row in rows], [3, 4])
        self.assertEqual([int(row['point_in_subsweep']) for row in rows], [0, 1])
        self.assertAlmostEqual(float(rows[0]['tick_time_s']), 1.21)

    def test_invalid_selection_and_missing_record(self):
        record = self.upload()
        url = f"/api/recordings/{record['id']}"
        for params in [{'frame_index': -1}, {'frame_index': 5}, {'sweep_index': 2}]:
            with self.subTest(params=params):
                self.assertEqual(self.client.get(url + '/frame', params=params).status_code, 422)
        self.assertEqual(self.client.get(url + '/frame', params={'sensor': 9}).status_code, 404)
        self.assertEqual(self.client.get(url + '/window', params={'start': 3, 'stop': 2}).status_code, 422)
        self.assertEqual(self.client.get('/api/recordings/missing').status_code, 404)
        self.assertEqual(self.client.get('/api/not-a-route').status_code, 404)

    def test_invalid_empty_and_oversize_uploads_do_not_fill_cache(self):
        for content in [b'', b'not hdf5']:
            self.assertEqual(self.client.post('/api/recordings', content=content).status_code, 422)
        with patch('a121_vitals.web.MAX_UPLOAD_BYTES', 100):
            self.assertEqual(self.client.post('/api/recordings', content=b'x' * 101).status_code, 413)
        self.assertEqual(len(self.store.records), 0)

    def test_external_links_and_large_decoded_datasets_are_rejected(self):
        external = self.path.with_name('external.h5')
        with h5py.File(external, 'w') as file:
            file['outside'] = h5py.ExternalLink(str(self.path), '/')
        self.assertEqual(self.client.post('/api/recordings', content=external.read_bytes()).status_code, 422)
        expanded = self.path.with_name('expanded.h5')
        with h5py.File(expanded, 'w') as file:
            file.create_dataset('huge', shape=(100_000_000,), dtype='i2', chunks=True, compression='gzip')
        self.assertLess(expanded.stat().st_size, 10000)
        self.assertEqual(self.client.post('/api/recordings', content=expanded.read_bytes()).status_code, 413)

    def test_deletion_expiration_and_capacity(self):
        self.store.max_records = 1
        record = self.upload()
        self.assertEqual(self.client.post('/api/recordings', content=self.path.read_bytes()).status_code, 503)
        self.store.records[record['id']].last_access -= 1801
        self.assertEqual(self.client.get(f"/api/recordings/{record['id']}").status_code, 404)
        record = self.upload()
        self.assertEqual(self.client.delete(f"/api/recordings/{record['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/recordings/{record['id']}").status_code, 404)

    def test_extended_session_group_sensor_selection(self):
        path = self.path.with_name('extended.h5')
        config = a121.SensorConfig(sweeps_per_frame=2, start_point=200, num_points=3)
        write_recording(path, [(config, 1), (a121.SessionConfig([{1: config, 2: config}, {1: config}]), 2)])
        response = self.client.post('/api/recordings', content=path.read_bytes())
        self.assertEqual(response.status_code, 201)
        record = response.json()
        with a121.open_record(path) as source:
            for group, sensor in [(0, 2), (1, 1)]:
                expected = source.session(1).extended_stacked_results[group][sensor][1].frame[1]
                value = self.client.get(f"/api/recordings/{record['id']}/frame", params={
                    'session': 1, 'group': group, 'sensor': sensor, 'frame_index': 1, 'sweep_index': 1,
                }).json()
                self.assertEqual(value['i'], expected.real.tolist())
                self.assertEqual(value['q'], expected.imag.tolist())

    def test_synthetic_example_and_static_frontend(self):
        response = self.client.post('/api/examples/demo')
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()['source'], 'synthetic')
        self.assertEqual(response.json()['summary']['sessions'][0]['streams'][0]['num_frames'], 600)
        timeline = self.client.get(f"/api/recordings/{response.json()['id']}/timeline").json()
        rates = timeline['breathing']['rates']
        self.assertEqual(len(rates), 600)
        self.assertIsNone(rates[0])
        self.assertAlmostEqual(rates[-1], 18, delta=1)
        sample = self.client.get('/api/examples/demo.h5')
        self.assertIn('synthetic-demo.h5', sample.headers['content-disposition'])
        imported = self.client.post('/api/recordings?filename=synthetic-demo.h5', content=sample.content)
        self.assertEqual(imported.status_code, 201, imported.text)
        imported_timeline = self.client.get(f"/api/recordings/{imported.json()['id']}/timeline").json()
        self.assertEqual(imported_timeline['breathing']['rates'], rates)
        directory = self.path.parent / 'web'
        directory.mkdir()
        (directory / 'index.html').write_text('<html><body>Viewer</body></html>')
        with TestClient(create_app(web_dir=directory)) as client:
            self.assertIn('Viewer', client.get('/').text)
            self.assertEqual(client.get('/api/unknown').status_code, 404)
            health = client.get('/api/health')
            self.assertEqual(health.headers['cache-control'], 'no-store')
            self.assertEqual(health.json()['playback_api'], 3)

    def test_bundled_sample_playback_without_download(self):
        self.assertEqual(self.client.get('/api/examples/sample.h5').status_code, 404)
        with h5py.File(SAMPLE_PATH, 'r') as sample_file:
            connection = json.loads(sample_file['client_info'][()])
            self.assertEqual(connection['serial']['port'], 'redacted')

        response = self.client.post('/api/examples/sample')
        self.assertEqual(response.status_code, 201, response.text)
        sample = response.json()
        self.assertEqual(sample['name'], '1109 data.h5')
        self.assertEqual(sample['source'], 'sample')

        record = f"/api/recordings/{sample['id']}"
        timeline = self.client.get(f'{record}/timeline').json()
        self.assertEqual(len(timeline['elapsed_s']), 1540)
        self.assertEqual(timeline['breathing']['reason'], 'missing_settings')
        frame = self.client.get(f'{record}/window?stop=1').json()
        self.assertEqual(len(frame['phase'][0]), 40)
        self.assertEqual(len(frame['mean_amplitude'][0]), 40)
        self.assertEqual(self.client.delete(record).status_code, 204)
        self.assertEqual(self.client.post('/api/examples/sample').json()['id'], sample['id'])

    def test_api_rejects_cross_origin_browser_requests(self):
        for headers in [
            {'Origin': 'https://outside.example'},
            {'Origin': 'http://testserver:8001'},
            {'Sec-Fetch-Site': 'same-site'},
            {'Sec-Fetch-Site': 'cross-site'},
        ]:
            with self.subTest(headers=headers):
                response = self.client.post('/api/examples/sample', headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertFalse(self.store.records)
        response = self.client.get('/api/health', headers={
            'Origin': 'http://testserver', 'Sec-Fetch-Site': 'same-origin',
        })
        self.assertEqual(response.status_code, 200)

    def test_local_server_rejects_untrusted_hosts(self):
        app = create_app(allowed_hosts=('localhost', '127.0.0.1', '[::1]'))
        with TestClient(app, base_url='http://localhost') as client:
            self.assertEqual(client.get('/api/health').status_code, 200)
            self.assertEqual(client.get('/api/health', headers={'Host': '[::1]:8000'}).status_code, 200)
            self.assertEqual(client.get('/api/health', headers={'Host': 'outside.example'}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
