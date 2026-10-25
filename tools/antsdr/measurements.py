"""Receive-only performance and bench analyses with explicit reference provenance."""
import json
import math
from pathlib import Path
import queue
import shutil
import threading
import time

import numpy as np

from .radio import Receiver
from .store import atomic_json, number, utc


def throughput(address, settings, rates, duration=5, channels=None, report=None, cancel=None, on_case=None):
    number(duration, .1, 3600, 'duration')
    if not rates or len(rates) > 32:
        raise ValueError('Choose 1–32 rates')
    for rate in rates:
        number(rate, 1e3, 61.44e6, 'rate')
    channel_sets = channels or [settings['channels']]
    result = {'kind': 'throughput', 'started_at_utc': utc(), 'cases': [], 'complete': False,
              'transmitted_samples': 0, 'wire_sample_format': 'sc16',
              'link_budget_note': 'sc16 requires 4 bytes per complex sample per channel, plus Ethernet/IP/UDP/CHDR overhead'}
    try:
        for selected in channel_sets:
            for rate in rates:
                if cancel and cancel.is_set():
                    return result
                case = {'requested_rate': rate, 'channels': selected, 'passed': False,
                        'payload_bits_per_second': rate * len(selected) * 32}
                result['cases'].append(case)
                receiver = None
                try:
                    config = dict(settings, channels=selected, sample_rate=rate)
                    config['channel_settings'] = {k: v for k, v in settings.get('channel_settings', {}).items() if int(k) in selected}
                    receiver = Receiver(address, config)
                    capture = receiver.run(duration, cancel=cancel)
                    case['capture'] = capture
                    case['passed'] = capture['complete'] and not any(capture[k] for k in ('overflows', 'timeouts', 'other_errors'))
                except Exception as error:
                    case['error'] = str(error)
                finally:
                    if receiver:
                        receiver.close()
                    if report:
                        atomic_json(report, result)
                print('Rate case:', selected, rate, 'passed=', case['passed'], flush=True)
                if on_case:
                    on_case(case)
        result['complete'] = True
        result['all_requested_rates_passed'] = all(case['passed'] for case in result['cases'])
        result['highest_passed_rate_by_channels'] = {
            ','.join(map(str, selected)): max((c['capture']['actual'][0]['sample_rate'] for c in result['cases']
                                               if c['channels'] == selected and c['passed']), default=None)
            for selected in channel_sets}
    finally:
        result['ended_at_utc'] = utc()
        if report:
            atomic_json(report, result)
    return result


def endurance(address, settings, duration, report, cancel=None, output=None, on_progress=None):
    number(duration, 1, 3600, 'duration')
    Path(report).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        planned = math.ceil(duration * settings['sample_rate'] * len(settings['channels']) * 8)
        if planned + 10*1024*1024 > shutil.disk_usage(Path(output).parent).free:
            raise ValueError('Insufficient free storage for the requested raw endurance recording')
    result = {'kind': 'endurance', 'started_at_utc': utc(), 'duration_seconds': duration,
              'complete': False, 'samples': [], 'transmitted_samples': 0}
    receiver = None
    last = 0
    pending = queue.Queue(maxsize=1)
    writer_errors = []
    def write_progress():
        try:
            while True:
                frame = pending.get()
                if frame is None:
                    return
                result['samples'].append({'host_utc': frame['time_utc'], 'telemetry': frame.get('telemetry'),
                                          'timing': frame.get('timing'), 'signal': frame.get('signal'),
                                          'stream_stats': frame.get('stream_stats'),
                                          'storage_free_bytes': shutil.disk_usage(Path(report).parent).free})
                atomic_json(report, result)
                if on_progress:
                    on_progress(frame)
        except BaseException as error:
            writer_errors.append(error)
    writer = None
    try:
        receiver = Receiver(address, settings)
        result['actual'] = receiver.actual
        writer = threading.Thread(target=write_progress, daemon=True)
        writer.start()
        def progress(frame):
            nonlocal last
            if writer_errors:
                raise RuntimeError('Endurance report writer failed: ' + str(writer_errors[0]))
            if time.monotonic() - last < 10:
                return
            last = time.monotonic()
            try:
                pending.put_nowait(frame)
            except queue.Full:
                pass  # A slow disk must not block sample consumption.
        try:
            capture = receiver.run(duration, output=output, cancel=cancel, on_spectrum=progress)
        finally:
            if writer.is_alive():
                pending.put(None, timeout=5)
                writer.join(timeout=5)
                if writer.is_alive():
                    raise RuntimeError('Endurance report writer did not stop')
            if writer_errors:
                raise RuntimeError('Endurance report writer failed: ' + str(writer_errors[0]))
        result['capture'] = capture
        result['complete'] = capture['complete'] and not any(capture[k] for k in ('overflows', 'timeouts', 'other_errors'))
    except BaseException as error:
        result['error'] = str(error) or type(error).__name__
        if receiver and receiver.last_report is not None:
            result['capture'] = receiver.last_report
        raise
    finally:
        try:
            if receiver:
                receiver.close()
        except BaseException as error:
            result.update(complete=False, cleanup_error=str(error))
            raise
        finally:
            result['ended_at_utc'] = utc()
            atomic_json(report, result)
    return result


def analyze_capture(source, tone_hz=None, reference_dbm=None, reference=None, step_at=None):
    """Measure a controlled recording; never invent absolute RF calibration."""
    source = Path(source)
    report = json.loads(source.with_suffix(source.suffix + '.json').read_text())
    if not report['complete'] or any(report[k] for k in ('overflows', 'timeouts', 'other_errors')):
        raise ValueError('Bench analysis requires a complete capture without receive errors')
    channels = len(report['actual'])
    count = report['saved_samples_per_channel']
    if source.stat().st_size != count * channels * 8 or not count:
        raise ValueError('Capture size differs from metadata')
    if reference_dbm is not None:
        number(reference_dbm, -200, 30, 'reference_dbm')
        if not isinstance(reference, str) or not 1 <= len(reference) <= 512:
            raise ValueError('Describe the calibrated source, attenuation, and reference plane')
    # Bound FFT and RAM; RMS/step traces cover the entire recording in chunks.
    samples = np.memmap(source, dtype='<c8', mode='r', shape=(count, channels))
    result = {'kind': 'receiver-bench', 'device_serial': report['device_serial'],
              'source_capture': str(source), 'reference': reference, 'channels': [],
              'absolute_levels_calibrated': reference_dbm is not None,
              'measurement_note': 'Tone frequency is baseband offset; RF gain is an index. Step/overload traces require a controlled input stimulus.'}
    rate = report['actual'][0]['sample_rate']
    if step_at is not None:
        number(step_at, 0, count/rate, 'step_at_seconds')
    if tone_hz is not None:
        number(tone_hz, -rate/2, rate/2, 'tone_hz')
    for channel, actual in enumerate(report['actual']):
        energy, clipped, trace, peak = 0., 0, [], 0.
        block_size = max(1024, int(rate * .001))
        block_size = max(block_size, math.ceil(count / 10000))
        for start in range(0, count, block_size):
            block = samples[start:start+block_size, channel]
            power = float(np.vdot(block, block).real)
            energy += power
            amplitude = np.maximum(np.abs(block.real), np.abs(block.imag))
            peak = max(peak, float(amplitude.max()))
            clipped += int(np.count_nonzero(amplitude >= 32767/32768))
            trace.append({'sample_start': start, 'time_seconds': start/rate,
                          'rms_dbfs': 10*math.log10(max(power/len(block), 1e-24))})
        length = min(65536, count)
        if tone_hz is not None and (abs(tone_hz) < 5*rate/length or abs(tone_hz) > rate/2-5*rate/length):
            raise ValueError('Tone/image measurements require separation from both DC and the Nyquist edge')
        block = np.asarray(samples[-length:, channel])
        window = np.hanning(length)
        fft = np.fft.fftshift(np.fft.fft(block*window)) / window.sum()
        window_noise_bandwidth_bins = length * float(np.dot(window, window)) / float(window.sum()**2)
        frequencies = np.fft.fftshift(np.fft.fftfreq(length, 1/rate))
        power = np.abs(fft)**2
        rms_dbfs = 10*math.log10(max(energy/count, 1e-24))
        item = {'channel': actual['channel'], 'rms_dbfs': rms_dbfs,
                'peak_component_dbfs': 20*math.log10(max(peak, 1e-12)),
                'clipped_samples': clipped, 'rms_trace': trace,
                'resolution_hz': rate/length, 'dc_dbfs': 10*math.log10(max(float(power[length//2]), 1e-24))}
        if reference_dbm is not None:
            item['level_correction_db'] = reference_dbm - rms_dbfs
            item['reference_input_dbm'] = reference_dbm
        if step_at is not None:
            after = [v for v in trace if v['time_seconds'] >= step_at]
            if len(after) < 3:
                raise ValueError('Record more samples after the controlled input step')
            target = float(np.median([v['rms_dbfs'] for v in after[-max(3,len(after)//5):]]))
            # Require the entire remainder of the measured trace to stay within
            # 1 dB of its final level, with at least 20 ms of post-settle data.
            last_outlier = max((i for i, v in enumerate(after) if abs(v['rms_dbfs']-target) > 1), default=-1)
            settled = after[last_outlier+1] if last_outlier+1 < len(after) else None
            latency = settled['time_seconds']-step_at if settled and after[-1]['time_seconds']-settled['time_seconds'] >= .02 else None
            item['step_response'] = {'stimulus_step_at_seconds': step_at, 'final_rms_dbfs': target,
                                     'settling_seconds': latency, 'tolerance_db': 1,
                                     'trace_resolution_seconds': block_size/rate,
                                     'note': 'Input step timing is supplied by the operator; input power is not inferred from output AGC level.'}
        if tone_hz is not None:
            wanted = int(np.argmin(np.abs(frequencies - tone_hz)))
            image = int(np.argmin(np.abs(frequencies + tone_hz)))
            # Integrating adjacent Hann bins includes the window's noise
            # bandwidth. Remove it so a bin-centered tone agrees with RMS.
            wanted_power = float(power[max(0,wanted-2):wanted+3].sum()) / window_noise_bandwidth_bins
            image_power = float(power[max(0,image-2):image+3].sum()) / window_noise_bandwidth_bins
            item['tone_offset_hz'] = float(frequencies[wanted])
            item['image_rejection_db'] = 10*math.log10(max(wanted_power,1e-24)/max(image_power,1e-24))
            item['tone_dbfs'] = 10*math.log10(max(wanted_power, 1e-24))
        result['channels'].append(item)
    result['overload_snapshots'] = report.get('telemetry')
    return result


def filter_response(passband, stopband, reference):
    if not isinstance(reference, str) or not 1 <= len(reference) <= 512:
        raise ValueError('Describe the equal-power source, offsets and reference plane')
    paths = [Path(passband), Path(stopband)]
    reports = [json.loads(p.with_suffix(p.suffix+'.json').read_text()) for p in paths]
    for r in reports:
        requested = r['requested']
        if requested.get('gain_mode', 'manual') != 'manual' or any(
                v.get('gain_mode', 'manual') != 'manual' for v in requested.get('channel_settings', {}).values()):
            raise ValueError('Filter rejection measurements require fixed manual gain')
    fields = ('frequency_hz', 'sample_rate', 'gain_db', 'bandwidth_hz', 'channel')
    for left, right in zip(reports[0]['actual'], reports[1]['actual']):
        if any(k not in left or k not in right or left[k] != right[k] for k in fields):
            raise ValueError('Filter recordings must use identical radio settings')
    if len(reports[0]['actual']) != len(reports[1]['actual']) or reports[0]['device_serial'] != reports[1]['device_serial']:
        raise ValueError('Filter recordings must use the same device and channels')
    analyzed = [analyze_capture(p) for p in paths]
    return {'kind': 'filter-response', 'reference': reference,
            'channels': [{'channel': a['channel'], 'rejection_db': a['rms_dbfs']-b['rms_dbfs']}
                         for a, b in zip(analyzed[0]['channels'], analyzed[1]['channels'])],
            'note': 'End-to-end RMS rejection for equal-power inputs; stopband measurement can be noise-floor limited.'}
