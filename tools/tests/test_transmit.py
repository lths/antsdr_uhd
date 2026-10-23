from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from antsdr.dashboard import Application
from antsdr.store import Store
from antsdr.transmit import DEFAULT, Transmitter, settings, waveform


class TransmitTest(unittest.TestCase):
    def test_invalid_settings_fail_before_device_creation(self):
        for change in ({'duration': 60}, {'gain_db': float('nan')},
                       {'channel': True}, {'tone_hz': 500000}, {'amplitude': 2},
                       {'sample_rate': 10e6, 'duration': 2}, {'unknown': 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                settings(change)

    def test_waveform_is_continuous_across_packet_boundaries(self):
        for kind in ('tone', 'two_tone', 'chirp'):
            config = settings({'waveform': kind})
            whole = waveform(config, 1e6, 0, 50000, 50000)
            chunks = [waveform(config, 1e6, n, min(4096, 50000-n), 50000)
                      for n in range(0, 50000, 4096)]
            np.testing.assert_array_equal(whole, np.concatenate(chunks))
            self.assertLessEqual(float(np.max(np.abs(whole))), config['amplitude'] + 1e-7)
            self.assertEqual(whole[0], 0)
            self.assertEqual(whole[-1], 0)

    def device(self):
        fake = MagicMock()
        device = fake.usrp.MultiUSRP.return_value
        device.get_tx_num_channels.return_value = 2
        device.get_tx_rate.return_value = 1e6
        device.get_time_now.return_value.get_real_secs.return_value = 100
        device.get_usrp_tx_info.return_value = {'mboard_serial': '0123456789abcdef'}
        for name in ('get_tx_freq_range', 'get_tx_bandwidth_range', 'get_tx_gain_range'):
            bounds = getattr(device, name).return_value
            bounds.start.return_value = 0
            bounds.stop.return_value = 10e9
        device.get_tx_freq.return_value = DEFAULT['frequency_hz']
        device.get_tx_bandwidth.return_value = 1e6
        device.get_tx_gain.return_value = 0
        fake.types.TimeSpec.side_effect = lambda value: value
        return fake, device, device.get_tx_stream.return_value

    def test_finite_burst_has_timed_start_and_acknowledged_end(self):
        fake, device, stream = self.device()
        sent = []
        def send(block, md, timeout):
            sent.append((len(block), md.start_of_burst, md.end_of_burst, md.has_time_spec, md.time_spec))
            return len(block)
        def acknowledge(event, timeout):
            event.event_code = 'burst_ack'
            return True
        stream.send.side_effect = send
        stream.recv_async_msg.side_effect = acknowledge
        with patch.dict('sys.modules', {'uhd': fake}):
            transmitter = Transmitter('192.168.1.10', {'duration': .006})
            report = transmitter.run(threading.Event())
            transmitter.close()
        self.assertTrue(report['complete'])
        self.assertEqual(report['samples_sent'], 6000)
        self.assertEqual(sent, [(4096, True, False, True, 100.1),
                                (1904, False, True, False, 100.1)])
        self.assertEqual(device.set_tx_gain.call_args_list[-1].args, (0, 0))

    def test_cancelled_schedule_never_sends_samples(self):
        fake, device, stream = self.device()
        cancel = threading.Event(); cancel.set()
        with patch.dict('sys.modules', {'uhd': fake}):
            transmitter = Transmitter('192.168.1.10', {})
            report = transmitter.run(cancel)
            transmitter.close()
        self.assertFalse(report['complete'])
        self.assertTrue(report['cancelled'])
        stream.send.assert_not_called()
        self.assertEqual(device.set_tx_gain.call_args_list[-1].args, (0, 0))

    def test_hardware_error_is_not_reported_as_success(self):
        fake, device, stream = self.device()
        stream.send.side_effect = lambda block, md, timeout: len(block)
        def fail(event, timeout):
            event.event_code = 'underflow'
            return True
        stream.recv_async_msg.side_effect = fail
        with patch.dict('sys.modules', {'uhd': fake}):
            transmitter = Transmitter('192.168.1.10', {})
            with self.assertRaisesRegex(RuntimeError, 'underflow'):
                transmitter.run(threading.Event())
            transmitter.close()
        self.assertEqual(device.set_tx_gain.call_args_list[-1].args, (0, 0))

    def test_gain_restore_failure_invalidates_an_acknowledged_burst(self):
        fake, device, stream = self.device()
        stream.send.side_effect = lambda block, md, timeout: len(block)
        def acknowledge(event, timeout):
            event.event_code = 'burst_ack'
            return True
        stream.recv_async_msg.side_effect = acknowledge
        with patch.dict('sys.modules', {'uhd': fake}):
            transmitter = Transmitter('192.168.1.10', {})
            device.set_tx_gain.side_effect = [None, RuntimeError('gain restore failed'), None]
            with self.assertRaisesRegex(RuntimeError, 'gain restore failed'):
                transmitter.run(threading.Event())
            self.assertFalse(transmitter.report['complete'])
            self.assertIn('gain restore failed', transmitter.report['error'])
            transmitter.close()

    def test_dashboard_excludes_other_jobs_and_saves_result(self):
        with tempfile.TemporaryDirectory() as root:
            app = Application(Store(root), '192.168.1.10', None)
            entered = threading.Event(); release = threading.Event()
            def run(cancel, progress):
                entered.set(); release.wait(2)
                return {'complete': True, 'samples_sent': 50000}
            with patch('antsdr.dashboard.Transmitter') as fake:
                fake.return_value.run.side_effect = run
                app.start({'kind': 'transmit', 'settings': {}})
                self.assertTrue(entered.wait(1))
                try:
                    with self.assertRaises(ValueError):
                        app.start({'kind': 'transmit', 'settings': {}})
                    app.stop()
                    self.assertTrue(app.cancel.is_set())
                finally:
                    release.set(); app.thread.join(2)
                self.assertEqual(app.snapshot()['job']['status'], 'complete')
                self.assertEqual(len(list((Path(root)/'transmit').glob('*.json'))), 1)
                fake.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
