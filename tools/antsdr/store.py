"""Validated, atomic configuration records shared by CLI and dashboard."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile


def utc():
    return datetime.now(timezone.utc).isoformat()


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValueError('Names must contain 1–64 letters, numbers, dots, underscores or hyphens')
    return value


def number(value, low, high, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('{} must be finite and within {}–{}'.format(field, low, high))
    return value


def profile(value):
    allowed = {'schema_version', 'name', 'frequency_hz', 'sample_rate', 'bandwidth_hz',
               'gain_db', 'channels', 'antenna', 'clock_source', 'calibration',
               'gain_mode', 'channel_settings', 'auto_dc_offset', 'auto_iq_balance',
               'lo_offset_hz', 'settle_seconds', 'fir_taps', 'time_source', 'utc_timestamps', 'reference_timeout_seconds'}
    if not isinstance(value, dict) or set(value)-allowed:
        raise ValueError('Unknown profile fields')
    value = dict(value)
    if value.get('schema_version') != 1:
        raise ValueError('Unsupported profile schema')
    name(value['name'])
    number(value['frequency_hz'], 1, 10e9, 'frequency_hz')
    number(value['sample_rate'], 1, 61.44e6, 'sample_rate')
    number(value['bandwidth_hz'], 1, 56e6, 'bandwidth_hz')
    number(value['gain_db'], 0, 76, 'gain_db')
    channels = value['channels']
    if not isinstance(channels, list) or not channels or any(type(c) is not int or c not in (0, 1) for c in channels) or len(set(channels)) != len(channels):
        raise ValueError('channels must be a unique list containing 0 and/or 1')
    if value.get('antenna', 'RX2') not in ('RX2', 'TX/RX'):
        raise ValueError('Unsupported receive antenna')
    if value.get('clock_source', 'internal') not in ('internal', 'external'):
        raise ValueError('Unsupported clock source')
    if value.get('time_source', 'internal') not in ('none', 'internal', 'external'):
        raise ValueError('Unsupported time source')
    if type(value.get('utc_timestamps', False)) is not bool:
        raise ValueError('utc_timestamps must be a boolean')
    if value.get('utc_timestamps') and value.get('time_source', 'internal') != 'external':
        raise ValueError('GPS UTC timestamps require the external PPS time source')
    if value.get('clock_source') == 'external' and value.get('time_source') == 'external':
        raise ValueError('E200 PPS and 10 MHz share one connector; select one external input mode')
    number(value.get('reference_timeout_seconds', 30), .1, 120, 'reference_timeout_seconds')
    if value.get('calibration'):
        name(value['calibration'])
    if value.get('gain_mode', 'manual') not in ('manual', 'slow', 'fast'):
        raise ValueError('Gain mode must be manual, slow or fast')
    for key in ('auto_dc_offset', 'auto_iq_balance'):
        if key in value and type(value[key]) is not bool:
            raise ValueError(key + ' must be a boolean')
    number(value.get('lo_offset_hz', 0), -28e6, 28e6, 'lo_offset_hz')
    number(value.get('settle_seconds', .05), 0, 1, 'settle_seconds')
    if value.get('lo_offset_hz') and abs(value['lo_offset_hz']) + value['sample_rate']/2 > value['bandwidth_hz']/2:
        raise ValueError('LO offset and sampled passband exceed analog bandwidth')
    overrides = value.get('channel_settings', {})
    if not isinstance(overrides, dict) or set(overrides) - {str(c) for c in channels}:
        raise ValueError('Channel settings must refer to selected channels')
    for override in overrides.values():
        if not isinstance(override, dict) or set(override)-{'gain_db', 'gain_mode', 'antenna'}:
            raise ValueError('Unknown channel setting')
        number(override.get('gain_db', value['gain_db']), 0, 76, 'gain_db')
        if override.get('gain_mode', value.get('gain_mode', 'manual')) not in ('manual', 'slow', 'fast'):
            raise ValueError('Unsupported channel gain mode')
        if override.get('antenna', value.get('antenna', 'RX2')) not in ('RX2', 'TX/RX'):
            raise ValueError('Unsupported channel antenna')
    if 'fir_taps' in value:
        taps = value['fir_taps']
        if not isinstance(taps, list) or not 16 <= len(taps) <= 128 or len(taps) % 16 or any(type(t) is not int or not -32768 <= t <= 32767 for t in taps):
            raise ValueError('FIR taps must be 16–128 signed int16 coefficients in multiples of 16')
    return value


def calibration(value):
    fields = {'schema_version', 'name', 'device_serial', 'measured_at_utc', 'reference',
              'frequency_error_ppm', 'level_correction_db'}
    if not isinstance(value, dict) or set(value) != fields or value['schema_version'] != 1:
        raise ValueError('Invalid calibration schema')
    name(value['name'])
    if not re.fullmatch(r'[0-9A-Fa-f]{8,64}', value['device_serial']):
        raise ValueError('Calibration requires a device serial')
    timestamp = datetime.fromisoformat(value['measured_at_utc'].replace('Z', '+00:00'))
    if timestamp.utcoffset() is None:
        raise ValueError('Calibration measurement time must include a timezone')
    if not isinstance(value['reference'], str) or not 1 <= len(value['reference']) <= 512:
        raise ValueError('Describe the measurement reference')
    number(value['frequency_error_ppm'], -200, 200, 'frequency_error_ppm')
    number(value['level_correction_db'], -100, 100, 'level_correction_db')
    return dict(value)


def atomic_json(path, value, *, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name, dir=str(path.parent))
    try:
        # Set access before publishing the inode. A chmod after replace leaves
        # readers a window where an otherwise valid status file is unreadable.
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Store:
    validators = {'profiles': profile, 'calibrations': calibration}

    def __init__(self, root):
        self.root = Path(root)

    def put(self, kind, value):
        value = self.validators[kind](value)
        atomic_json(self.root / kind / (name(value['name'])+'.json'), value)
        return value

    def get(self, kind, key):
        return self.validators[kind](json.loads((self.root / kind / (name(key)+'.json')).read_text()))

    def list(self, kind):
        return [self.get(kind, path.stem) for path in sorted((self.root/kind).glob('*.json'))]
