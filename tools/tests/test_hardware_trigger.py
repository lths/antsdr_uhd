import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from antsdr.__main__ import DEFAULT
from antsdr.radio import Receiver


class HardwareTriggerTest(unittest.TestCase):
    def radio(self):
        fake = MagicMock()
        fake.types.RXMetadataErrorCode = SimpleNamespace(none='none', timeout='timeout', overflow='overflow')
        fake.types.StreamMode = SimpleNamespace(num_done='finite', start_cont='continuous', stop_cont='stop')
        fake.types.StreamCMD.side_effect = lambda mode: SimpleNamespace(mode=mode)
        fake.types.TimeSpec.side_effect = lambda value: SimpleNamespace(get_real_secs=lambda: value)
        device = fake.usrp.MultiUSRP.return_value
        device.get_usrp_rx_info.return_value = {'mboard_serial': '0123456789abcdef'}
        device.get_rx_num_channels.return_value = 2
        device.get_rx_rate.return_value = 1e6
        device.get_rx_freq.return_value = DEFAULT['frequency_hz']
        device.get_rx_gain.return_value = DEFAULT['gain_db']
        device.get_rx_bandwidth.return_value = DEFAULT['bandwidth_hz']
        fake.types.RXMetadata.return_value.time_spec.get_real_secs.return_value = 100
        device.get_time_now.return_value.get_real_secs.return_value = 100
        device.get_time_last_pps.return_value.get_real_secs.return_value = 100
        for getter in ('get_rx_freq_range', 'get_rx_gain_range', 'get_rx_bandwidth_range'):
            getattr(device, getter).return_value.start.return_value = 0
            getattr(device, getter).return_value.stop.return_value = 10e9
        stream = device.get_rx_stream.return_value
        stream.get_max_num_samps.return_value = 4096
        return fake, device, stream

    def test_level_arguments_reach_fpga_and_dual_level_is_rejected(self):
        fake, device, stream = self.radio()
        with patch.dict('sys.modules', {'uhd': fake}):
            receiver = Receiver('192.168.1.10', DEFAULT, trigger_mode='fpga-level', hardware_threshold_dbfs=-6)
            args = device.get_rx_stream.call_args.args[0]
            self.assertEqual(args.args, 'trigger=level,trigger_level=16423')
            receiver.close()
            device.reset_mock()
            with self.assertRaisesRegex(ValueError, 'one RX channel'):
                Receiver('192.168.1.10', dict(DEFAULT, channels=[0, 1]), trigger_mode='fpga-level', hardware_threshold_dbfs=-6)
            device.get_rx_stream.assert_not_called()

    def test_device_time_is_sent_to_fpga_and_capture_stops_at_exact_sample_count(self):
        fake, device, stream = self.radio()
        values = iter([0, 4096, 4096, 1808])
        received = 0
        def recv(buffer, metadata, timeout):
            nonlocal received
            self.assertEqual(buffer.shape, (1, 16384))
            n = next(values)
            metadata.error_code = 'none' if n else 'timeout'
            metadata.time_spec.get_real_secs.return_value = 100.5+received/1e6
            buffer[:, :n] = .25
            received += n
            return n
        stream.recv.side_effect = recv
        with patch.dict('sys.modules', {'uhd': fake}):
            receiver = Receiver('192.168.1.10', DEFAULT, trigger_mode='device-time')
            report = receiver.run(.01, device_delay_seconds=.5)
            receiver.close()
        command = stream.issue_stream_cmd.call_args_list[0].args[0]
        self.assertEqual(command.mode, 'finite')
        self.assertFalse(command.stream_now)
        self.assertEqual(command.time_spec.get_real_secs(), 100.5)
        self.assertEqual(command.num_samps, 10000)
        self.assertTrue(report['complete'])
        self.assertEqual(report['saved_samples_per_channel'], 10000)
        self.assertEqual(report['first_sample_device_time'], 100.5)
        self.assertEqual(report['timeouts'], 0)

    def test_missing_external_edge_times_out_without_false_trigger_or_success(self):
        fake, device, stream = self.radio()
        def waiting(buffer, metadata, timeout):
            time.sleep(.005)
            metadata.error_code = 'timeout'
            return 0
        stream.recv.side_effect = waiting
        with patch.dict('sys.modules', {'uhd': fake}):
            receiver = Receiver('192.168.1.10', DEFAULT, trigger_mode='external-edge')
            report = receiver.run(.01, trigger_timeout=.1, cancel=threading.Event())
            receiver.close()
        self.assertFalse(report['triggered'])
        self.assertFalse(report['complete'])
        self.assertEqual(report['saved_samples_per_channel'], 0)
        self.assertEqual(report['timeouts'], 0)
        self.assertEqual(stream.issue_stream_cmd.call_args_list[-1].args[0].mode, 'stop')

    def test_host_capture_with_overflow_does_not_report_success(self):
        fake, device, stream = self.radio()
        errors = iter(['overflow', 'none'])
        def recv(buffer, metadata, timeout):
            metadata.error_code = next(errors)
            buffer.fill(np.complex64(.25))
            return 4096 if metadata.error_code == 'none' else 0
        stream.recv.side_effect = recv
        ticks = iter(n / 100 for n in range(100))
        with patch.dict('sys.modules', {'uhd': fake}), patch('antsdr.radio.time.monotonic', side_effect=lambda: next(ticks)):
            receiver = Receiver('192.168.1.10', DEFAULT)
            report = receiver.run(.01)
            receiver.close()
        self.assertTrue(report['triggered'])
        self.assertGreater(report['saved_samples_per_channel'], 0)
        self.assertEqual(report['overflows'], 1)
        self.assertFalse(report['complete'])

    def test_capture_stop_failure_is_recorded_as_incomplete(self):
        fake, device, stream = self.radio()
        def recv(buffer, metadata, timeout):
            metadata.error_code = 'none'
            buffer[:] = .25
            return 4096
        def command(value):
            if value.mode == 'stop':
                raise RuntimeError('stop failed')
        stream.recv.side_effect = recv
        stream.issue_stream_cmd.side_effect = command
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / 'capture.cf32'
            ticks = iter(n / 100 for n in range(100))
            with patch.dict('sys.modules', {'uhd': fake}), patch('antsdr.radio.time.monotonic', side_effect=lambda: next(ticks)):
                receiver = Receiver('192.168.1.10', DEFAULT)
                with self.assertRaisesRegex(RuntimeError, 'stop failed'):
                    receiver.run(.01, output=output)
                receiver.close()
            report = json.loads(output.with_suffix('.cf32.json').read_text())
        self.assertFalse(report['complete'])
        self.assertIn('stop failed', report['error'])

    def test_control_failure_stops_capture_and_preserves_partial_statistics(self):
        fake, device, stream = self.radio()
        with patch.dict('sys.modules', {'uhd': fake}):
            receiver = Receiver('192.168.1.10', DEFAULT)
            receiver.telemetry = {'errors': [{'channel': 0, 'error': 'control response lost'}]}
            with self.assertRaisesRegex(RuntimeError, 'Receiver telemetry failed'):
                receiver.run(.1)
            self.assertFalse(receiver.last_report['complete'])
            self.assertIn('control response lost', receiver.last_report['error'])
            self.assertEqual(receiver.last_report['received_samples_per_channel'], 0)
            stream.recv.assert_not_called()
            receiver.close()

    def test_cancellation_during_final_receive_does_not_report_success(self):
        fake, device, stream = self.radio()
        cancel = threading.Event()
        sizes = iter([4096, 4096, 1808])
        def recv(buffer, metadata, timeout):
            count = next(sizes)
            metadata.error_code = 'none'
            buffer[:, :count] = .25
            if count == 1808:
                cancel.set()
            return count
        stream.recv.side_effect = recv
        with patch.dict('sys.modules', {'uhd': fake}):
            receiver = Receiver('192.168.1.10', DEFAULT, trigger_mode='device-time')
            report = receiver.run(.01, device_delay_seconds=.5, cancel=cancel)
            receiver.close()
        self.assertEqual(report['saved_samples_per_channel'], 10000)
        self.assertTrue(report['cancelled'])
        self.assertFalse(report['complete'])


if __name__ == '__main__':
    unittest.main()
