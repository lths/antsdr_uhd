#!/usr/bin/env python3
"""Manual E200 physical PPS regression. Requires a connected 1 PPS source; never transmits."""
import argparse
import ipaddress
from pathlib import Path
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from antsdr.__main__ import DEFAULT
from antsdr.radio import Receiver
from antsdr.store import atomic_json
import uhd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', type=ipaddress.IPv4Address, default='192.168.1.10')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("x") as output:
        output.write("{}\n")
    report = {'passed': False, 'transmitted_samples': 0, 'sources': [], 'cases': []}
    receiver = None
    original = None
    try:
        receiver = Receiver(str(args.address), dict(DEFAULT, frequency_hz=902050000),
                            trigger_mode='external-edge')
        device = receiver.device
        original = device.get_time_source(0)
        report['clock_source'] = device.get_clock_source(0)
        report['device_serial'] = receiver.serial
        for source, duration in [('none', 1.2), ('internal', 2.2), ('external', 10.2)]:
            device.set_time_source(source, 0)
            time.sleep(.1)
            last = device.get_time_last_pps(0).get_real_secs()
            events = []
            start = time.monotonic()
            while time.monotonic() - start < duration:
                value = device.get_time_last_pps(0).get_real_secs()
                if value != last:
                    events.append({'device_seconds': value,
                                   'host_elapsed_seconds': time.monotonic() - start})
                    last = value
                time.sleep(.01)
            intervals = [b['device_seconds'] - a['device_seconds']
                         for a, b in zip(events, events[1:])]
            record = {'source': source, 'observation_seconds': time.monotonic() - start,
                      'events': events, 'intervals_seconds': intervals}
            report['sources'].append(record)
            print(source, len(events), 'edges; intervals', intervals, flush=True)
            if source == 'none':
                assert not events, 'PPS changed while disabled'
            elif source == 'internal':
                assert 2 <= len(events) <= 3 and all(.999 < d < 1.001 for d in intervals)
            else:
                assert 9 <= len(events) <= 11, 'No steady external 1 PPS'
                assert all(.999 < d < 1.001 for d in intervals), intervals
        device.set_time_unknown_pps(uhd.types.TimeSpec(0.0))
        report['synchronized_last_pps'] = device.get_time_last_pps(0).get_real_secs()
        report['synchronized_now'] = device.get_time_now(0).get_real_secs()
        assert 0 <= report['synchronized_last_pps'] < 2
        assert 0 <= report['synchronized_now'] < 2
        print('PPS time synchronization passed', flush=True)
        device.set_time_source(original, 0)
        device = None
        receiver.close()
        receiver = None
        for channels in ([0], [1], [0, 1]):
            receiver = Receiver(str(args.address), dict(DEFAULT, frequency_hz=902050000,
                                channels=channels), trigger_mode='external-edge')
            device = receiver.device
            original = device.get_time_source(0)
            device.set_time_source('external', 0)
            time.sleep(.1)
            captures = []
            case = {'channels': channels, 'captures': captures, 'passed': False}
            report['cases'].append(case)
            for index in range(3):
                result = receiver.run(.025, trigger_timeout=2.5)
                captures.append(result)
                assert result['complete'] and result['triggered'], result
                assert result['saved_samples_per_channel'] == 25000, result
                assert not any(result[k] for k in ('overflows', 'timeouts', 'other_errors'))
                pps = device.get_time_last_pps(0).get_real_secs()
                result['pps_time_after_capture'] = pps
                result['first_sample_offset_from_pps_seconds'] = result['first_sample_device_time'] - pps
                assert abs(result['first_sample_offset_from_pps_seconds']) < 2 / receiver.rate, result
                print('channels', channels, 'capture', index, 'samples=25000, PPS offset us=',
                      result['first_sample_offset_from_pps_seconds'] * 1e6, flush=True)
            starts = [c['first_sample_device_time'] for c in captures]
            case['start_intervals_seconds'] = [b - a for a, b in zip(starts, starts[1:])]
            assert all(.999 < d < 1.001 for d in case['start_intervals_seconds']), case
            case['passed'] = True
            device.set_time_source(original, 0)
            device = None
            receiver.close()
            receiver = None
        report['passed'] = True
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        try:
            if receiver is not None:
                try:
                    if original is not None:
                        receiver.device.set_time_source(original, 0)
                finally:
                    device = None
                    receiver.close()
        except BaseException as error:
            report.update(passed=False, cleanup_error=str(error))
            raise
        finally:
            atomic_json(args.report, report)
            print('PPS verification passed:', report['passed'], flush=True)


if __name__ == "__main__":
    main()
