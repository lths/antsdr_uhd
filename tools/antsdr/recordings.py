"""Loss-aware SigMF export of existing little-endian ANTSDR I/Q captures."""
import hashlib
import json
import os
from pathlib import Path


def export_sigmf(source, destination):
    source, destination = Path(source), Path(destination)
    report = json.loads(source.with_suffix(source.suffix + '.json').read_text())
    channels = len(report['actual'])
    count = report['saved_samples_per_channel']
    if source.stat().st_size != count * channels * 8:
        raise ValueError('Capture length does not match sample/channel metadata')
    if not channels or not count:
        raise ValueError('Cannot export an empty capture')
    meta = Path(str(destination) + '.sigmf-meta')
    data = Path(str(destination) + '.sigmf-data')
    meta.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(meta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    created_data = False
    try:
        # Copy, rather than link, so subsequent edits to one recording cannot
        # silently invalidate the other recording's hash or sample metadata.
        with source.open('rb') as src, os.fdopen(os.open(data, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as dst:
            created_data = True
            digest = hashlib.sha512()
            for chunk in iter(lambda: src.read(1024 * 1024), b''):
                dst.write(chunk)
                digest.update(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if data.stat().st_size != count * channels * 8:
            raise ValueError('Capture length changed during export')
        capture = {'core:sample_start': 0, 'core:frequency': report['actual'][0]['frequency_hz']}
        if report.get('first_saved_sample_utc'):
            capture['core:datetime'] = report['first_saved_sample_utc']
        metadata = {'global': {'core:version': '1.2.6', 'core:datatype': 'cf32_le',
                              'core:sample_rate': report['actual'][0]['sample_rate'],
                              'core:num_channels': channels, 'core:sha512': digest.hexdigest(),
                              'core:recorder': 'ANTSDR UHD operations tools',
                              'core:extensions': [{'name': 'antsdr', 'version': '1.0.0', 'optional': True}],
                              'antsdr:complete': bool(report['complete']),
                              'antsdr:channels': [c['channel'] for c in report['actual']],
                              'antsdr:trigger': report.get('trigger_kind'),
                              'antsdr:time_source': report.get('time_source', 'internal'),
                              'antsdr:clock_source': report.get('clock_source', 'internal'),
                              'antsdr:errors': {k: report.get(k, 0) for k in ('overflows', 'timeouts', 'other_errors')},
                              'antsdr:discontinuities': report.get('discontinuities', [])},
                    'captures': [capture], 'annotations': []}
        with os.fdopen(descriptor, 'w') as stream:
            descriptor = None
            json.dump(metadata, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        return {'metadata_file': str(meta), 'data_file': str(data), 'samples_per_channel': count,
                'channels': channels, 'complete': bool(report['complete']),
                'utc_tagged': 'core:datetime' in capture}
    except BaseException:
        if created_data:
            data.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
