#!/usr/bin/env python3
"""Manual E200 hardware-trigger regression. Sends no transmit samples."""
import argparse
import ipaddress
from pathlib import Path

from antsdr.__main__ import DEFAULT
from antsdr.radio import Receiver
from antsdr.store import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', type=ipaddress.IPv4Address, default='192.168.1.10')
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--expect-external-timeout', action='store_true',
                        help='Also test an unconnected external PPS input; disconnect the pulse source first')
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x') as output:
        output.write('{}\n')
    report = {'schema_version': 1, 'transmitted_samples': 0, 'passed': False, 'cases': []}
    cases = [('device-time', [0], 1e6, None, True),
             ('device-time', [1], 5e6, None, True),
             ('device-time', [0, 1], 1e6, None, True),
             ('fpga-level', [0], 1e6, -90, True),
             ('fpga-level', [1], 1e6, -90, True),
             ('fpga-level', [0], 1e6, 0, False)]
    if args.expect_external_timeout:
        cases.extend([('external-edge', [0], 1e6, None, False),
                      ('external-edge', [0, 1], 1e6, None, False)])
    try:
        for mode, channels, rate, threshold, expect_samples in cases:
            receiver = Receiver(str(args.address), dict(DEFAULT, frequency_hz=902050000,
                channels=channels, sample_rate=rate), trigger_mode=mode,
                hardware_threshold_dbfs=threshold)
            try:
                result = receiver.run(.025, trigger_timeout=.5,
                    device_delay_seconds=.2 if mode == 'device-time' else None)
                result['test_channels'] = channels
                result['passed'] = (result['complete'] == expect_samples
                    and result['triggered'] == expect_samples
                    and result['saved_samples_per_channel'] == (round(receiver.rate*.025) if expect_samples else 0)
                    and not any(result[key] for key in ('overflows', 'timeouts', 'other_errors')))
                if mode == 'device-time':
                    result['passed'] &= 0 <= result['first_sample_device_time']-result['scheduled_device_time'] < .001
                report['cases'].append(result)
                if not result['passed']:
                    raise RuntimeError('Hardware trigger case failed: '+mode+' '+str(channels))
                print('Passed '+mode+' '+str(channels)+' at '+str(receiver.rate)+' samples/s', flush=True)
            finally:
                receiver.close()
                atomic_json(args.report, report)
        report['passed'] = True
    finally:
        atomic_json(args.report, report)


if __name__ == '__main__':
    main()
