#!/usr/bin/env python3
"""Manual E200 receiver-control regression. Never creates or sends a TX stream."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tools'))
from antsdr.__main__ import DEFAULT
from antsdr.radio import Receiver
from antsdr.store import atomic_json
from antsdr import tuning


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', default='192.168.1.10')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = {'passed': False, 'cases': []}
    base = dict(DEFAULT, frequency_hz=902050000)
    cases = [
        ('manual-dual', {'channels': [0, 1], 'channel_settings': {'0': {'gain_db': 12}, '1': {'gain_db': 23}}}),
        ('slow-agc', {'gain_mode': 'slow'}),
        ('fast-agc', {'gain_mode': 'fast'}),
        ('mixed-agc', {'channels': [0, 1], 'gain_mode': 'slow', 'channel_settings': {'1': {'gain_mode': 'fast'}}}),
        ('lo-offset', {'bandwidth_hz': 2e6, 'lo_offset_hz': 100000}),
        ('iq-off', {'auto_iq_balance': False, 'auto_dc_offset': True}),
        ('dc-off', {'auto_iq_balance': True, 'auto_dc_offset': False}),
        ('fir-zero', {'fir_taps': [0]*128}),
        ('fir-restored', {}),
        ('telemetry-5msps', {'sample_rate': 5e6}),
        ('telemetry-10msps', {'sample_rate': 10e6}),
    ]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='receiver-', dir=args.report.parent) as temporary:
            for label, change in cases:
                config = dict(base, **change)
                record = {'case': label, 'passed': False}
                result['cases'].append(record)
                radio = None
                try:
                    radio = Receiver(args.address, config, trigger_mode='device-time')
                    # Inspect shared tracking independently of host request values.
                    status = tuning.telemetry(radio.device, config['channels'])
                    assert not status['errors'], status
                    for channel in status['channels']:
                        assert channel['auto_dc_offset'] == config.get('auto_dc_offset', True), channel
                        assert channel['auto_iq_balance'] == config.get('auto_iq_balance', True), channel
                        desired = tuning.channel_settings(config, channel['channel'])
                        assert channel['gain_mode'] == desired.get('gain_mode', 'manual'), channel
                        if desired.get('gain_mode', 'manual') == 'manual':
                            assert channel['gain_index'] == desired['gain_db'], channel
                    output = Path(temporary) / (label + '.cf32') if label.startswith('fir-') else None
                    capture = radio.run(1.2 if label.startswith('telemetry-') else .05,
                                        output=output, device_delay_seconds=.2)
                    record['capture'] = capture
                    assert capture['complete'] and not capture['overflows'] and not capture['other_errors'], capture
                    assert capture['telemetry'] and not capture['telemetry']['errors'], capture
                    if output:
                        peak = float(np.max(np.abs(np.fromfile(output, dtype='<c8'))))
                        record['max_amplitude'] = peak
                        assert (peak == 0) if label == 'fir-zero' else (peak > 0), peak
                    if label == 'lo-offset':
                        rf = radio.device.get_tree().access_double(tuning.frontend(0) + '/freq/value').get()
                        assert abs(rf - (base['frequency_hz'] + 100000)) < 10, rf
                        assert abs(radio.device.get_rx_freq(0) - base['frequency_hz']) < 10
                    record['passed'] = True
                    print(label + ': passed', flush=True)
                except BaseException as error:
                    record['error'] = str(error)
                    raise
                finally:
                    if radio:
                        radio.close()
        result['passed'] = True
    finally:
        atomic_json(args.report, result)


if __name__ == '__main__':
    main()
