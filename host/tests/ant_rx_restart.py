#!/usr/bin/env python3
"""Manual E200 RX restart regression. Creates TX streamers but sends no TX samples.

Run in the host container with exclusive radio access. Deliberately stop reading
an RX stream long enough to exhaust its flow-control credits, close it, then
require the next capture to start on time without stale data or metadata errors.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import time

import numpy as np
import uhd


def receive(stream, rate, channels, samples, report):
    metadata = uhd.types.RXMetadata()
    buffer = np.empty((len(channels), 4096), np.complex64)
    deadline = time.monotonic() + 4
    report.update(samples=0, errors=[], first_timestamp=None)
    while report['samples'] < samples:
        if time.monotonic() >= deadline:
            raise RuntimeError('Capture deadline exceeded')
        count = stream.recv(buffer, metadata, 0.5)
        if metadata.error_code != uhd.types.RXMetadataErrorCode.none:
            error = {'code': str(metadata.error_code),
                     'out_of_sequence': metadata.out_of_sequence,
                     'at_sample': report['samples']}
            report['errors'].append(error)
            raise RuntimeError('Receive metadata error: ' + str(error))
        if not count:
            raise RuntimeError('Empty receive without metadata error')
        if not metadata.has_time_spec:
            raise RuntimeError('Missing device timestamp')
        stamp = metadata.time_spec.get_real_secs()
        if report['first_timestamp'] is None:
            report['first_timestamp'] = stamp
            # Reset the clock only after final streamer/MIMO setup. Old data
            # from the previous session must not precede the requested burst.
            if not 0.1 <= stamp <= 0.101:
                raise RuntimeError('First timestamp belongs to another capture: ' + str(stamp))
        expected = report['first_timestamp'] + report['samples'] / rate
        if abs(stamp - expected) > 2 / rate:
            raise RuntimeError('Discontinuous device timestamps')
        report['samples'] += count


def capture(address, channels, requested_rate, backlog, duplex, report):
    device = stream = tx_stream = None
    try:
        hint = ',master_clock_rate=16e6' if backlog else ''
        device = uhd.usrp.MultiUSRP('type=ant,addr=' + address + hint)
        # Constructing a TX streamer exercises the duplex setup path. Never
        # call send(); also leave its hardware gain at the minimum setting.
        for channel in channels:
            device.set_tx_gain(device.get_tx_gain_range(channel).start(), channel)
            device.set_rx_rate(requested_rate, channel)
            device.set_rx_freq(uhd.types.TuneRequest(902050000), channel)
            device.set_rx_bandwidth(1e6, channel)
            device.set_rx_gain(10, channel)
            device.set_rx_antenna('RX2', channel)
        args = uhd.usrp.StreamArgs('fc32', 'sc16')
        args.channels = channels
        stream = device.get_rx_stream(args)
        if duplex:
            if len(channels) != 1:
                raise ValueError('Use a single channel for duplex setup')
            device.set_tx_rate(requested_rate, channels[0])
            tx_stream = device.get_tx_stream(args)
        rate = device.get_rx_rate(channels[0])
        report.update(channels=channels, rate=rate, duplex=duplex,
                      deliberate_unread_seconds=0.15 if backlog else 0,
                      serial=device.get_usrp_rx_info(channels[0]).get('mboard_serial'))
        if any(device.get_rx_rate(channel) != rate for channel in channels):
            raise RuntimeError('Channel sample rates differ')
        device.set_time_now(uhd.types.TimeSpec(0))
        command = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont if backlog
                                      else uhd.types.StreamMode.num_done)
        command.stream_now = False
        command.time_spec = uhd.types.TimeSpec(0.1)
        samples = 32768 if backlog else 100000
        if not backlog:
            command.num_samps = samples
        stream.issue_stream_cmd(command)
        receive(stream, rate, channels, samples, report)
        if backlog:
            time.sleep(0.15)
        report['passed'] = True
    finally:
        if stream is not None:
            stream.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
        tx_stream = stream = device = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', type=ipaddress.IPv4Address, default='192.168.1.10')
    parser.add_argument('--iterations', type=int, default=6)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100:
        parser.error('--iterations must be between 1 and 100')
    if args.report.exists():
        parser.error('Report exists; choose a new path')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    cases = [([0], 1e6), ([1], 1e6), ([0, 1], 1e6),
             ([0], 5e6), ([1], 10e6), ([0, 1], 1e6)]
    report = {'schema_version': 1, 'test': 'E200 RX restart after flow-control backlog',
              'transmitted_samples': 0, 'passed': False, 'cases': []}
    # Reserve the output before opening the device; never overwrite old results.
    with args.report.open('x') as output:
        json.dump(report, output, indent=2)
    try:
        for index in range(args.iterations):
            channels, rate = cases[index % len(cases)]
            case = {'seed': {}, 'next_capture': {}}
            report['cases'].append(case)
            capture(str(args.address), [index % 2], 1e6, True,
                    index % 2 == 0, case['seed'])
            capture(str(args.address), channels, rate, False, False, case['next_capture'])
            print('Passed restart {}: {} at {} samples/s'.format(index + 1, channels, rate),
                  flush=True)
            args.report.write_text(json.dumps(report, indent=2) + '\n')
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        args.report.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
