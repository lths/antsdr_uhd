#!/usr/bin/env python3
"""Manual runtime-2 ownership regression; sends no RF samples.

Requires exclusive access to an idle E200. Exercises device-side contention,
renewal, release, timeout, and the FPGA's control-packet gate independently of
the host process lock. No SSH credentials or administrator access are needed.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import socket
import struct
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--address', type=ipaddress.IPv4Address, default='192.168.1.10')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Report exists; choose a new path')
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {'test': 'E200 device ownership', 'passed': False, 'transmitted_samples': 0}
    nonce = 0
    token = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as owner, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as competitor, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        for sock in (owner, competitor):
            sock.connect((str(args.address), 49101))
            sock.settimeout(1)
        control.connect((str(args.address), 49200))
        control.settimeout(.2)

        def lease(sock, operation, presented=0):
            nonlocal nonce
            nonce += 1
            sock.send(struct.pack('!8I', 0x414c5331, 1, operation, nonce,
                                 presented >> 32, presented & 0xffffffff, 0, 0))
            result = struct.unpack('!8I', sock.recv(128))
            if result[:4] != (0x414c5331, 1, operation, nonce):
                raise RuntimeError('Invalid lease reply')
            return result

        def peek(expected):
            # Little-endian CHDR: local SID 0x40, readback selector 32/index 0.
            control.send(bytes.fromhex('10000080400000002000000000000000'))
            try:
                reply = control.recv(256)
            except socket.timeout:
                if expected:
                    raise RuntimeError('Owner control request was blocked')
                return
            if not expected:
                raise RuntimeError('Control packet passed without a lease')
            header, sid = struct.unpack('<2I', reply[:8])
            if header & 0xffff != len(reply) or sid != 0x00400000 or \
                    struct.unpack('<2I', reply[-8:]) != (0xace0ba5e, 0x00100000):
                raise RuntimeError('Invalid FPGA compatibility reply')

        try:
            if lease(owner, 0)[6]:
                raise RuntimeError('Device already owned; close other radio sessions')
            peek(False)
            grant = lease(owner, 1)
            if grant[6]:
                raise RuntimeError('Acquire rejected')
            token = grant[4] << 32 | grant[5]
            peek(True)
            busy = lease(competitor, 1)
            assert busy[4:7] == (0, 0, 1)
            assert lease(competitor, 2, token ^ 1)[6] == 2
            assert lease(owner, 2, token)[6] == 0
            assert lease(owner, 3, token)[6] == 0
            token = 0
            peek(False)
            time.sleep(.1)
            grant = lease(owner, 1)
            assert grant[6] == 0
            # Deliberately omit renew and release, like a disconnected host.
            start = time.monotonic()
            while lease(competitor, 0)[6]:
                if time.monotonic() - start > 3.6:
                    raise RuntimeError('Device did not expire a lost host')
                time.sleep(.05)
            report['expiry_seconds'] = time.monotonic() - start
            peek(False)
            report['passed'] = True
        finally:
            if token:
                lease(owner, 3, token)
            args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
