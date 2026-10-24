import json
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from antsdr.__main__ import DEFAULT
from antsdr.dashboard import Application
from antsdr.store import Store, profile
from antsdr import tuning


class ReceiverTuningTest(unittest.TestCase):
    def device(self):
        device, uhd = MagicMock(), MagicMock()
        device.get_rx_gain_range.return_value.start.return_value = 0
        device.get_rx_gain_range.return_value.stop.return_value = 76
        device.get_rx_sensor_names.return_value = []
        device.get_rx_gain.return_value = 42
        device.get_rx_filter_names.return_value = []
        device.get_tree.return_value.exists.return_value = True
        return device, uhd

    def test_profile_validates_modes_shared_controls_channel_overrides_and_fir(self):
        valid = dict(DEFAULT, channels=[0, 1], gain_mode='slow',
                     channel_settings={'1': {'gain_mode': 'fast', 'gain_db': 23}},
                     auto_iq_balance=False, lo_offset_hz=100000, bandwidth_hz=2e6,
                     fir_taps=[1]*16)
        self.assertEqual(profile(valid), valid)
        for change in ({'gain_mode': 'hybrid'}, {'auto_dc_offset': 1},
                       {'channel_settings': {'2': {}}}, {'channels': [[0]]},
                       {'channel_settings': {'0': {'frequency_hz': 100e6}}},
                       {'channel_settings': {'0': {'gain_mode': 'invalid'}}},
                       {'channel_settings': {'0': {'gain_db': float('nan')}}},
                       {'lo_offset_hz': 1e6}, {'fir_taps': [1]*17}, {'fir_taps': [40000]*16}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                profile(dict(DEFAULT, **change))

    def test_per_channel_agc_and_gain_follow_shared_tracking(self):
        device, uhd = self.device()
        config = profile(dict(DEFAULT, channels=[0, 1], gain_mode='slow', settle_seconds=0,
                              auto_iq_balance=False, channel_settings={'1': {'gain_mode': 'manual', 'gain_db': 23}}))
        tuning.configure(device, uhd, config, 1e9)
        device.set_rx_iq_balance.assert_called_once_with(False, 0)
        self.assertEqual(device.set_rx_agc.call_args_list[-1].args, (True, 0))
        self.assertEqual(device.set_rx_gain.call_args_list[-1].args, (23, 1))
        device.get_tree.return_value.access_str.return_value.set.assert_called_once_with('slow')
        # Both manual settings must be seeded before the first AGC enable.
        self.assertLess(device.mock_calls.index(('set_rx_agc', (False, 1), {})),
                        device.mock_calls.index(('set_rx_agc', (True, 0), {})))

    def test_lo_offset_and_lock_timeout_are_explicit(self):
        device, uhd = self.device()
        config = dict(DEFAULT, bandwidth_hz=2e6, lo_offset_hz=100000, settle_seconds=0)
        tuning.configure(device, uhd, config, 1e9)
        uhd.types.TuneRequest.assert_called_once_with(1e9, 100000)
        device.get_rx_sensor_names.return_value = ['lo_locked']
        device.get_rx_sensor.return_value.to_bool.return_value = False
        with patch('antsdr.tuning.time.monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, 'failed to lock'):
                tuning.configure(device, uhd, config, 1e9)

    def test_fir_incompatible_channel_prevents_any_filter_write(self):
        device, uhd = self.device()
        device.get_rx_filter_names.return_value = ['AD9361/FIR_1']
        first, second = MagicMock(), MagicMock()
        first.is_bypassed.return_value = second.is_bypassed.return_value = False
        first.get_taps.return_value = [1]*16
        second.get_taps.return_value = [1]*32
        device.get_rx_filter.side_effect = [first, second]
        with self.assertRaisesRegex(ValueError, 'tap count'):
            tuning.configure(device, uhd, dict(DEFAULT, channels=[0, 1], fir_taps=[1]*16), 1e9)
        device.set_rx_filter.assert_not_called()

    def test_telemetry_decodes_overload_and_keeps_shared_state_readback(self):
        device, uhd = self.device()
        device.get_rx_sensor_names.return_value = ['rssi', 'lo_locked', 'gain_index', 'overload']
        def sensor(name, channel):
            value = MagicMock()
            value.to_real.return_value = {'rssi': -55.5, 'gain_index': 42, 'overload': 5}.get(name, 0)
            value.to_bool.return_value = True
            return value
        device.get_rx_sensor.side_effect = sensor
        device.get_tree.return_value.access_bool.return_value.get.return_value = False
        result = tuning.telemetry(device, [0])
        json.dumps(result, allow_nan=False)
        channel = result['channels'][0]
        self.assertEqual(channel['gain_index'], 42)
        self.assertTrue(channel['overload_flags']['digital_saturation'])
        self.assertTrue(channel['overload_flags']['large_adc'])
        self.assertFalse(channel['auto_iq_balance'])
        self.assertFalse(result['errors'])

    def test_inspection_is_exclusive_and_closes_the_radio(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root); store.put('profiles', DEFAULT)
            app = Application(store, '192.168.1.10', None)
            with patch('antsdr.dashboard.Receiver') as receiver:
                receiver.return_value.describe.return_value = {'complete': True, 'capabilities': {}}
                app.start({'kind': 'inspect', 'profile': 'e200-rx'})
                app.thread.join(2)
                self.assertEqual(app.snapshot()['job']['status'], 'complete')
                receiver.return_value.run.assert_not_called()
                receiver.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
