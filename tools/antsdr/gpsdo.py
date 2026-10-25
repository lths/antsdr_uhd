"""Read-only LBE-1421 status/NMEA bridge; no RF or UHD device ownership."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import select
import struct
import time

from .store import atomic_json


def devices():
    found = []
    for entry in Path('/sys/class/hidraw').glob('hidraw*'):
        fields = dict(line.split('=', 1) for line in (entry/'device/uevent').read_text().splitlines() if '=' in line)
        if fields.get('HID_ID', '').upper() != '0003:00001DD2:00002444':
            continue
        parent = next((p for p in (entry/'device').resolve().parents if (p/'idVendor').exists()), None)
        serial = None
        for tty in Path('/sys/class/tty').glob('ttyACM*'):
            if parent and parent in (tty/'device').resolve().parents:
                serial = '/dev/' + tty.name
        found.append({'hid': '/dev/' + entry.name, 'serial': serial,
                      'identity': fields.get('HID_UNIQ', '')})
    return found


class Bodnar:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            info = bytearray(8)
            fcntl.ioctl(self.fd, (2 << 30) | (8 << 16) | (ord('H') << 8) | 3, info, True)
            _, vendor, product = struct.unpack('Ihh', info)
            if (vendor & 65535, product & 65535) != (0x1dd2, 0x2444):
                raise ValueError('Expected an LBE-1421 HID interface')
        except BaseException:
            self.close()
            raise

    def close(self):
        os.close(self.fd)

    def status(self):
        data = bytearray(60)
        data[0] = 0x4b
        count = fcntl.ioctl(self.fd, (3 << 30) | (60 << 16) | (ord('H') << 8) | 7, data, True)
        if count < 21:
            raise ValueError('Truncated GPSDO status')
        return {'gps_locked': bool(data[1] & 1), 'pll_locked': bool(data[1] & 2),
                'antenna_ok': bool(data[1] & 4), 'pps_enabled': bool(data[1] & 128),
                'out1_frequency_hz': struct.unpack('<I', data[6:10])[0],
                'out2_frequency_hz': struct.unpack('<I', data[14:18])[0],
                'updated_monotonic': time.monotonic()}

    def set_pps(self, enabled):
        command = bytearray(60)
        command[0:2] = bytes([12, int(enabled)])
        fcntl.ioctl(self.fd, (3 << 30) | (60 << 16) | (ord('H') << 8) | 6, command, True)
        if self.status()['pps_enabled'] != enabled:
            raise RuntimeError('PPS selection readback failed')


def nmea(sentence, received=None):
    """Return status/UTC only; deliberately exclude position coordinates."""
    if not sentence.startswith('$') or '*' not in sentence:
        return None
    body, checksum = sentence[1:].strip().rsplit('*', 1)
    check = 0
    for byte in body.encode('ascii'):
        check ^= byte
    if check != int(checksum, 16):
        return None
    fields = body.split(',')
    if fields[0].endswith('RMC') and len(fields) >= 10:
        if fields[2] != 'A':
            return {'rmc_valid': False}
        stamp = datetime.strptime(fields[9] + fields[1].split('.')[0], '%d%m%y%H%M%S').replace(tzinfo=timezone.utc)
        # RMC may be emitted halfway through the second (LBE-1421). Its
        # integral UTC epoch identifies that second's leading PPS edge.
        fraction = float('0.' + fields[1].split('.', 1)[1]) if '.' in fields[1] else 0
        if not 0 <= fraction <= .7:
            return None
        return {'rmc_valid': True, 'rmc_utc': stamp.isoformat().replace('+00:00', 'Z'),
                'rmc_fraction_seconds': fraction,
                'rmc_received_monotonic': time.monotonic() if received is None else received}
    if fields[0].endswith('GGA') and len(fields) > 8:
        return {'fix_quality': int(fields[6] or 0), 'satellites_used': int(fields[7] or 0)}
    return None


def snapshot(path):
    try:
        value = json.loads(Path(path).read_text())
        age = time.monotonic() - value['updated_monotonic']
        value['age_seconds'] = age
        value['available'] = 0 <= age < 3
        if not value['available']:
            value['rmc_valid'] = False
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {'available': False, 'error': 'GPSDO bridge unavailable'}


def bridge(hid, serial, destination):
    device = Bodnar(hid)
    fd = None
    try:
        fd = os.open(serial, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY) if serial else None
        if fd is not None:
            # Discard queued NMEA before associating live UTC with PPS edges.
            for _ in range(32):
                if not select.select([fd], [], [], 0)[0]:
                    break
                os.read(fd, 8192)
        buffer = b''
        state = {'rmc_valid': False}
        deadline = 0
        while True:
            if fd is not None and select.select([fd], [], [], .1)[0]:
                chunk = os.read(fd, 8192)
                if not chunk:
                    raise OSError('GPS serial interface disconnected')
                buffer = (buffer + chunk)[-16384:]
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        value = nmea(line.decode('ascii').strip())
                        if value:
                            if value.get('rmc_utc') == state.get('rmc_utc'):
                                value.pop('rmc_received_monotonic', None)
                            state.update(value)
                    except (ValueError, UnicodeError, IndexError):
                        pass
            elif fd is None:
                time.sleep(.1)
            if time.monotonic() >= deadline:
                state.update(device.status())
                # Public status contains no credentials or position. Publish
                # its read permissions atomically with the new contents.
                atomic_json(destination, state, mode=0o644)
                deadline = time.monotonic() + .2
    finally:
        if fd is not None:
            os.close(fd)
        device.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--devices', action='store_true')
    parser.add_argument('--hid')
    parser.add_argument('--serial')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--pps', choices=('on', 'off'))
    args = parser.parse_args()
    if args.devices:
        matches = devices()
        if len(matches) != 1:
            raise ValueError('Connect exactly one LBE-1421')
        print(matches[0]['hid'])
        if matches[0]['serial']:
            print(matches[0]['serial'])
        return
    if args.state:
        bridge(args.hid, args.serial, args.state)
    else:
        device = Bodnar(args.hid)
        try:
            if args.pps:
                device.set_pps(args.pps == 'on')
            print(json.dumps(device.status(), indent=2))
        finally:
            device.close()


if __name__ == '__main__':
    main()
