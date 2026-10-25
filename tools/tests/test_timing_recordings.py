from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from antsdr.gpsdo import nmea, snapshot
from antsdr.timing import Timing
from antsdr.recordings import export_sigmf
from antsdr.measurements import analyze_capture, filter_response, endurance
from antsdr.scan import validate
from antsdr.store import profile
from antsdr.__main__ import DEFAULT
from antsdr.dashboard import Application
from antsdr.store import Store


def sentence(body):
    checksum = 0
    for byte in body.encode():
        checksum ^= byte
    return '$' + body + '*' + format(checksum, '02X')


class TimingTest(unittest.TestCase):
    def test_nmea_half_second_and_invalid_fix_checksum(self):
        body = 'GNRMC,140400.50,A,1234.56,N,12345.67,W,0.0,0.0,251026,,,A'
        value = nmea(sentence(body), 100.5)
        self.assertEqual(value['rmc_utc'], '2026-10-25T14:04:00Z')
        self.assertEqual(value['rmc_fraction_seconds'], .5)
        self.assertNotIn('latitude', value)
        self.assertIsNone(nmea(sentence(body)[:-2] + '00', 100.5))
        self.assertFalse(nmea(sentence(body.replace(',A,', ',V,', 1)), 100.5)['rmc_valid'])

    def clock(self):
        with patch('antsdr.timing.threading.Thread'):
            return Timing(MagicMock(), 'external')

    def gps(self, second, received):
        return {'available': True, 'gps_locked': True, 'pps_enabled': True, 'rmc_valid': True,
                'rmc_utc': '2026-10-25T14:04:' + format(second, '02d') + 'Z',
                'rmc_received_monotonic': received}

    def associated(self):
        clock = self.clock()
        clock.observe(0, 99, {})
        clock.observe(10, 100.01, self.gps(0, 100.5))
        clock.observe(10, 100.55, self.gps(0, 100.5))
        clock.observe(11, 101.01, self.gps(0, 100.5))
        clock.observe(11, 101.55, self.gps(1, 101.5))
        return clock

    def test_two_epochs_required_and_device_utc_mapping(self):
        clock = self.clock()
        clock.observe(0, 99, {})
        clock.observe(10, 100.01, self.gps(0, 100.5))
        with patch('antsdr.timing.time.monotonic', return_value=100.55):
            self.assertFalse(clock.status()['utc_valid'])
        clock = self.associated()
        with patch('antsdr.timing.time.monotonic', return_value=101.55):
            self.assertTrue(clock.status()['utc_valid'])
            self.assertEqual(clock.utc(11.125), '2026-10-25T14:04:01.125000Z')
        with patch('antsdr.timing.time.monotonic', return_value=104):
            self.assertFalse(clock.status()['utc_valid'])

    def test_loss_ambiguous_rmc_fast_edges_and_time_reset_invalidate_utc(self):
        for change in ('gps-loss', 'old-rmc', 'fast', 'reset', 'missing'):
            clock = self.associated()
            gps = self.gps(2, 102.5)
            if change == 'gps-loss': gps['gps_locked'] = False
            if change == 'old-rmc': gps['rmc_received_monotonic'] = 100
            clock.observe(11.1 if change == 'fast' else 0 if change == 'reset' else 13 if change == 'missing' else 12,
                          102.55, gps)
            with patch('antsdr.timing.time.monotonic', return_value=102.55):
                self.assertFalse(clock.status()['utc_valid'], change)
                self.assertEqual(clock.status()['utc_losses'], 1, change)
        clock = self.clock()
        for i in range(5): clock.observe(i*.01, 100+i*.01, {})
        with patch('antsdr.timing.time.monotonic', return_value=100.05):
            self.assertFalse(clock.status()['pps_healthy'])

    def test_profile_rejects_shared_connector_conflict_and_utc_without_pps(self):
        for fields in ({'utc_timestamps': True}, {'time_source': 'bad'},
                       {'utc_timestamps': 1}, {'clock_source': 'external', 'time_source': 'external'}):
            with self.assertRaises(ValueError): profile(dict(DEFAULT, **fields))

    def test_missing_bridge_is_explicit(self):
        self.assertFalse(snapshot('/not/a/timing/file')['available'])


class RecordingTest(unittest.TestCase):
    def capture(self, root, channels=2):
        path = Path(root)/'recording.cf32'
        count, rate = 4096, 1e6
        tone = np.exp(2j*np.pi*62500*np.arange(count)/rate).astype('<c8') * .125
        samples = np.column_stack([tone*(i+1) for i in range(channels)]).astype('<c8')
        samples.tofile(path)
        report = {'complete': True, 'saved_samples_per_channel': count, 'device_serial': '0123456789abcdef',
                  'actual': [{'channel': i, 'frequency_hz': 902e6, 'sample_rate': rate,
                              'gain_db': 10, 'bandwidth_hz': 1e6} for i in range(channels)],
                  'overflows': 0, 'timeouts': 0, 'other_errors': 0,
                  'first_saved_sample_utc': '2026-10-25T14:04:01.125000Z'}
        path.with_suffix('.cf32.json').write_text(json.dumps(report))
        return path, report

    def test_export_preserves_interleaved_data_hash_utc_and_incomplete_flag(self):
        import hashlib
        with tempfile.TemporaryDirectory() as root:
            path, report = self.capture(root)
            report['complete'] = False
            report['overflows'] = 1
            path.with_suffix('.cf32.json').write_text(json.dumps(report))
            result = export_sigmf(path, Path(root)/'export')
            meta = json.loads(Path(result['metadata_file']).read_text())
            self.assertEqual(Path(result['data_file']).read_bytes(), path.read_bytes())
            self.assertEqual(Path(result['data_file']).stat().st_mode & 0o777, 0o600)
            self.assertEqual(meta['global']['core:num_channels'], 2)
            self.assertEqual(meta['global']['core:datatype'], 'cf32_le')
            self.assertEqual(meta['global']['core:sha512'], hashlib.sha512(path.read_bytes()).hexdigest())
            self.assertEqual(meta['captures'][0]['core:datetime'], report['first_saved_sample_utc'])
            self.assertFalse(meta['global']['antsdr:complete'])
            self.assertEqual(meta['global']['antsdr:errors']['overflows'], 1)
            with self.assertRaises(FileExistsError): export_sigmf(path, Path(root)/'export')

    def test_export_does_not_overwrite_dataset_and_rejects_truncation(self):
        with tempfile.TemporaryDirectory() as root:
            path, report = self.capture(root)
            data = Path(root)/'export.sigmf-data'
            data.write_bytes(b'existing')
            with self.assertRaises(FileExistsError): export_sigmf(path, Path(root)/'export')
            self.assertEqual(data.read_bytes(), b'existing')
            self.assertFalse((Path(root)/'export.sigmf-meta').exists())
            path.write_bytes(b'short')
            with self.assertRaises(ValueError): export_sigmf(path, Path(root)/'other')

    def test_bench_tone_image_rms_and_reference_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            path, report = self.capture(root, 1)
            result = analyze_capture(path, 62500, -50, 'Calibrated source at RX input after 40 dB pad')
            channel = result['channels'][0]
            self.assertAlmostEqual(channel['rms_dbfs'], 20*np.log10(.125), places=4)
            self.assertAlmostEqual(channel['tone_dbfs'], channel['rms_dbfs'], places=4)
            self.assertGreater(channel['image_rejection_db'], 80)
            self.assertEqual(channel['clipped_samples'], 0)
            self.assertAlmostEqual(channel['level_correction_db'], -50-channel['rms_dbfs'])
            with self.assertRaises(ValueError): analyze_capture(path, 62500, -50)
            report['overflows'] = 1
            path.with_suffix('.cf32.json').write_text(json.dumps(report))
            with self.assertRaises(ValueError): analyze_capture(path)

    def test_scan_rejects_unbounded_incompatible_or_multichannel_requests(self):
        for frequencies, dwell, cycles, config in [([], .025, 1, DEFAULT),
                ([902e6]*9, .025, 1, DEFAULT), ([100e6, 902e6], .025, 1, DEFAULT),
                ([902e6], .025, True, DEFAULT), ([902e6], .025, 1, dict(DEFAULT, channels=[0,1])),
                ([902e6], .025, 1, dict(DEFAULT, lo_offset_hz=100000)),
                ([902e6], 1, 1, dict(DEFAULT, sample_rate=10e6))]:
            with self.assertRaises(ValueError): validate(frequencies, dwell, cycles, config)

    def test_step_settling_and_filter_comparison_require_controlled_settings(self):
        with tempfile.TemporaryDirectory() as root:
            path, report = self.capture(root, 1)
            report['requested'] = dict(DEFAULT)
            count, rate = 100000, 1e6
            signal = np.exp(2j*np.pi*62500*np.arange(count)/rate).astype('<c8')*.125
            signal[:25000] *= .1
            signal.tofile(path)
            report['saved_samples_per_channel'] = count
            path.with_suffix('.cf32.json').write_text(json.dumps(report))
            result = analyze_capture(path, step_at=.02)
            step = result['channels'][0]['step_response']
            self.assertLess(abs(step['settling_seconds']-.005), step['trace_resolution_seconds'])
            second = Path(root)/'stop.cf32'
            (signal*.01).tofile(second)
            second.with_suffix('.cf32.json').write_text(json.dumps(report))
            comparison = filter_response(path, second, 'Equal-power test source at the RX connector')
            self.assertAlmostEqual(comparison['channels'][0]['rejection_db'], 40, places=3)
            report['requested']['gain_mode'] = 'fast'
            second.with_suffix('.cf32.json').write_text(json.dumps(report))
            with self.assertRaises(ValueError): filter_response(path, second, 'source')

    def test_endurance_rejects_insufficient_disk_before_opening_radio(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root, patch('antsdr.measurements.Receiver') as receiver:
            with patch('antsdr.measurements.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
                with self.assertRaises(ValueError):
                    endurance('192.168.1.10', DEFAULT, 3600, Path(root)/'report.json', output=Path(root)/'data.cf32')
            receiver.assert_not_called()


class EnduranceFailureTest(unittest.TestCase):
    def test_checkpoint_disk_io_does_not_block_receive_callback(self):
        import threading
        from antsdr.store import atomic_json
        started, release = threading.Event(), threading.Event()
        receive_thread = threading.get_ident()
        def slow_write(path, value):
            if threading.get_ident() != receive_thread:
                started.set()
                if not release.wait(2):
                    raise RuntimeError('Receive callback blocked on checkpoint I/O')
            atomic_json(path, value)
        def receive(duration, **options):
            options['on_spectrum']({'time_utc': '2026-10-25T14:04:01Z'})
            self.assertTrue(started.wait(1))
            release.set()
            return {'complete': True, 'overflows': 0, 'timeouts': 0, 'other_errors': 0}
        with tempfile.TemporaryDirectory() as root, patch('antsdr.measurements.Receiver') as receiver:
            receiver.return_value.actual = [{'channel': 0, 'sample_rate': 1e6}]
            receiver.return_value.run.side_effect = receive
            with patch('antsdr.measurements.atomic_json', side_effect=slow_write):
                result = endurance('192.168.1.10', DEFAULT, 10, Path(root)/'report.json')
            self.assertTrue(result['complete'])
            self.assertEqual(len(result['samples']), 1)

    def test_failed_endurance_keeps_capture_statistics_and_exception_type(self):
        with tempfile.TemporaryDirectory() as root, patch('antsdr.measurements.Receiver') as receiver:
            receiver.return_value.actual = [{'channel': 0, 'sample_rate': 1e6}]
            receiver.return_value.run.side_effect = KeyboardInterrupt()
            receiver.return_value.last_report = {'complete': False, 'overflows': 2,
                                                  'received_samples_per_channel': 1234}
            report = Path(root)/'report.json'
            with self.assertRaises(KeyboardInterrupt):
                endurance('192.168.1.10', DEFAULT, 10, report)
            saved = json.loads(report.read_text())
            self.assertFalse(saved['complete'])
            self.assertEqual(saved['error'], 'KeyboardInterrupt')
            self.assertEqual(saved['capture']['overflows'], 2)
            receiver.return_value.close.assert_called_once()


class MeasurementDashboardTest(unittest.TestCase):
    def test_bounded_jobs_validation_and_exclusion(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.put('profiles', DEFAULT)
            app = Application(store, '192.168.1.10', None)
            invalid = [dict(kind='scan', frequencies=[100e6,902e6]),
                       dict(kind='scan', frequencies=[902e6], cycles=True),
                       dict(kind='throughput', rates=[float('nan')]),
                       dict(kind='throughput', rates=[1e6], all_channels='yes'),
                       dict(kind='soak', duration=3601), dict(kind='soak', rates=[1e6])]
            with patch('antsdr.measurements.Receiver') as receiver:
                for request in invalid:
                    with self.assertRaises(ValueError): app.start(dict(profile='e200-rx', **request))
                receiver.assert_not_called()
            with patch('antsdr.measurements.throughput', return_value={'complete': True}) as sweep:
                app.start({'kind': 'throughput', 'profile': 'e200-rx', 'rates': [1e6], 'duration': .1})
                app.thread.join(2)
                self.assertEqual(app.snapshot()['job']['status'], 'complete')
                sweep.assert_called_once()
            with patch('antsdr.scan.run', return_value={'complete': False, 'cancelled': True}):
                app.start({'kind': 'scan', 'profile': 'e200-rx', 'frequencies': [902e6]})
                app.thread.join(2)
                self.assertEqual(app.snapshot()['job']['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
