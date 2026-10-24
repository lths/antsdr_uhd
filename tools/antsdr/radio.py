"""Receive only: bounded streaming, triggered capture and calibrated spectra."""
from collections import deque
import json
import math
from pathlib import Path
import threading
import time

from .store import atomic_json, number, profile, utc
from . import tuning


def spectrum(samples, rate, frequency, bins=1024, level_correction_db=0):
    import numpy as np
    if bins < 16 or bins > 8192 or bins & (bins-1):
        raise ValueError('FFT bins must be a power of two within 16–8192')
    signal = np.asarray(samples).reshape(-1)
    if len(signal) < bins:
        raise ValueError('Not enough samples for FFT')
    window = np.hanning(bins)
    transformed = np.fft.fftshift(np.fft.fft(signal[-bins:]*window)) / window.sum()
    power = 20*np.log10(np.maximum(np.abs(transformed), 1e-12)) + level_correction_db
    return {'frequency_start_hz': frequency-rate/2, 'bin_width_hz': rate/bins,
            'power_dbfs': power.tolist(), 'level_correction_db': level_correction_db}


class Trigger:
    def __init__(self, rate, pretrigger_seconds, threshold_dbfs=None, hold=False):
        import numpy as np
        self.np = np
        self.limit = int(number(pretrigger_seconds, 0, 5, 'pretrigger_seconds')*rate)
        self.threshold = threshold_dbfs
        self.hold = hold
        if threshold_dbfs is not None:
            number(threshold_dbfs, -160, 0, 'threshold_dbfs')
        self.blocks = deque()
        self.count = 0
        self.fired = False

    def accept(self, block, force=False):
        if self.fired:
            return [block]
        rms = float(self.np.sqrt(self.np.mean(self.np.abs(block[0])**2)))
        if force or (self.threshold is None and not self.hold) or (self.threshold is not None and 20*math.log10(max(rms, 1e-12)) >= self.threshold):
            self.fired = True
            result = list(self.blocks)+[block]
            self.blocks.clear()
            self.count = 0
            return result
        if self.limit:
            self.blocks.append(block.copy())
            self.count += block.shape[1]
            while self.count > self.limit:
                excess = self.count-self.limit
                first = self.blocks.popleft()
                if first.shape[1] > excess:
                    self.blocks.appendleft(first[:, excess:])
                    self.count -= excess
                else:
                    self.count -= first.shape[1]
        return []


class Receiver:
    def __init__(self, address, settings, calibration=None, trigger_mode='host', hardware_threshold_dbfs=None):
        import uhd
        self.uhd = uhd
        self.settings = profile(settings)
        if trigger_mode not in ('host', 'device-time', 'fpga-level', 'external-edge'):
            raise ValueError('Unsupported trigger mode')
        if trigger_mode == 'fpga-level':
            number(hardware_threshold_dbfs, -90, 0, 'hardware_threshold_dbfs')
            if len(self.settings['channels']) != 1:
                raise ValueError('FPGA level triggering currently requires one RX channel')
        elif hardware_threshold_dbfs is not None:
            raise ValueError('Hardware threshold requires FPGA level triggering')
        self.trigger_mode = trigger_mode
        self.hardware_threshold_dbfs = hardware_threshold_dbfs
        self.calibration = calibration
        self.device = self.stream = None
        self.telemetry = None
        try:
            self._initialize(address)
        except BaseException:
            self.close()
            raise

    def _initialize(self, address):
        uhd = self.uhd
        settings = self.settings
        self.device = uhd.usrp.MultiUSRP('type=ant,addr='+address)
        if any(c >= self.device.get_rx_num_channels() for c in settings['channels']):
            raise ValueError('Requested channel is unavailable')
        self.serial = self.device.get_usrp_rx_info(settings['channels'][0]).get('mboard_serial', '')
        if self.calibration and self.calibration['device_serial'].upper() != self.serial.upper():
            raise ValueError('Calibration serial does not match the connected radio')
        self.device.set_clock_source(settings.get('clock_source', 'internal'))
        ppm = self.calibration['frequency_error_ppm'] if self.calibration else 0
        tune = settings['frequency_hz']/(1+ppm/1e6)
        for channel in settings['channels']:
            for value, bounds, field in (
                    (tune + settings.get('lo_offset_hz', 0), self.device.get_rx_freq_range(channel), 'frequency'),
                    (settings['bandwidth_hz'], self.device.get_rx_bandwidth_range(channel), 'bandwidth')):
                if not bounds.start() <= value <= bounds.stop():
                    raise ValueError('{} exceeds the connected radio capabilities'.format(field))
            self.device.set_rx_rate(settings['sample_rate'], channel)
        stream_args = uhd.usrp.StreamArgs('fc32', 'sc16')
        stream_args.channels = settings['channels']
        if self.trigger_mode in ('fpga-level', 'external-edge'):
            trigger_args = 'trigger=' + ('level' if self.trigger_mode == 'fpga-level' else 'external')
            if self.trigger_mode == 'fpga-level':
                trigger_args += ',trigger_level=' + str(max(1, round(32768*10**(self.hardware_threshold_dbfs/20))))
            stream_args.args = trigger_args
        self.stream = self.device.get_rx_stream(stream_args)
        # MIMO setup may reconfigure the chip's clock and restore defaults.
        # Apply RF controls and custom taps only after final stream activation.
        tuning.configure(self.device, uhd, settings, tune)
        self.rate = self.device.get_rx_rate(settings['channels'][0])
        if any(self.device.get_rx_rate(c) != self.rate for c in settings['channels']):
            raise ValueError('Selected channels have different actual sample rates')
        if len(settings['channels']) > 1:
            self.device.set_time_now(uhd.types.TimeSpec(0.0))
        self.actual = [{'channel': c, 'frequency_hz': self.device.get_rx_freq(c),
                        'sample_rate': self.device.get_rx_rate(c),
                        'gain_db': self.device.get_rx_gain(c),
                        'gain_mode': tuning.channel_settings(settings, c).get('gain_mode', 'manual'),
                        'bandwidth_hz': self.device.get_rx_bandwidth(c)} for c in settings['channels']]

    def describe(self):
        return {'complete': True, 'requested': self.settings, 'actual': self.actual,
                'capabilities': tuning.capabilities(self.device, self.settings['channels']),
                'telemetry': tuning.telemetry(self.device, self.settings['channels'])}

    def close(self):
        self.stream = None
        self.device = None

    def run(self, duration, output=None, threshold_dbfs=None, pretrigger_seconds=0,
            trigger_timeout=10, on_spectrum=None, cancel=None, bins=1024, trigger_after_seconds=None,
            device_delay_seconds=None):
        import numpy as np
        number(duration, 0.01, 3600, 'duration')
        number(trigger_timeout, 0.1, 3600, 'trigger_timeout')
        if self.trigger_mode != 'host':
            if threshold_dbfs is not None or trigger_after_seconds is not None or pretrigger_seconds:
                raise ValueError('Hardware triggers cannot use host triggers or host pretrigger history')
            if self.trigger_mode == 'device-time':
                number(device_delay_seconds, .1, trigger_timeout, 'device_delay_seconds')
            elif device_delay_seconds is not None:
                raise ValueError('Device delay requires device-time triggering')
        elif device_delay_seconds is not None:
            raise ValueError('Device delay requires device-time triggering')
        if trigger_after_seconds is not None:
            number(trigger_after_seconds, 0, trigger_timeout, 'trigger_after_seconds')
            if threshold_dbfs is not None:
                raise ValueError('Choose either a level trigger or a delayed trigger')
        # Bound buffer memory independently of requested sample rate/channels.
        if pretrigger_seconds*self.rate*len(self.settings['channels'])*8 > 256*1024*1024:
            raise ValueError('Pretrigger buffer exceeds 256 MiB')
        trigger = Trigger(self.rate, pretrigger_seconds, threshold_dbfs, hold=trigger_after_seconds is not None)
        metadata = self.uhd.types.RXMetadata()
        count = max(1024, min(16384, int(self.stream.get_max_num_samps())))
        buffer = np.empty((len(self.settings['channels']), count), dtype=np.complex64)
        hardware_samples = round(duration*self.rate) if self.trigger_mode != 'host' else None
        start_cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.num_done if hardware_samples
                                            else self.uhd.types.StreamMode.start_cont)
        if hardware_samples:
            if hardware_samples > (1 << 28)-1:
                raise ValueError('Hardware capture exceeds the FPGA finite-command sample limit')
            start_cmd.num_samps = hardware_samples
        start_cmd.stream_now = self.trigger_mode == 'host' and len(self.settings['channels']) == 1
        if not start_cmd.stream_now:
            start_cmd.time_spec = self.uhd.types.TimeSpec(self.device.get_time_now().get_real_secs()+
                (device_delay_seconds if self.trigger_mode == 'device-time' else .1))
        report = {'schema_version':1, 'started_at_utc':utc(), 'device_serial':self.serial,
                  'requested':self.settings, 'actual':self.actual, 'calibration':self.calibration,
                  'format':'little-endian complex float32, sample-major interleaved channels',
                  'received_samples_per_channel':0, 'saved_samples_per_channel':0,
                  'overflows':0, 'timeouts':0, 'other_errors':0, 'triggered':False,
                  'first_sample_device_time':None, 'complete':False, 'cancelled':False,
                  'discontinuities':[]}
        report['trigger_kind'] = 'host-delay' if trigger_after_seconds is not None else 'level' if threshold_dbfs is not None else 'immediate'
        if self.trigger_mode != 'host':
            report['trigger_kind'] = self.trigger_mode
            report['hardware_threshold_dbfs'] = self.hardware_threshold_dbfs
            report['scheduled_device_time'] = start_cmd.time_spec.get_real_secs()
        report['device_time_reset_for_alignment'] = len(self.settings['channels']) > 1
        handles = None
        if output:
            output = Path(output)
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if output.exists() or output.with_suffix(output.suffix+'.json').exists():
                raise ValueError('Capture destination already exists')
            import os
            handles = os.fdopen(os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb')
        start = time.monotonic()
        finish = None
        last_frame = 0
        clipped = [0 for _ in self.settings['channels']]
        peaks = [0.0 for _ in self.settings['channels']]
        monitor_stop = threading.Event()
        def monitor():
            while not monitor_stop.is_set():
                self.telemetry = tuning.telemetry(self.device, self.settings['channels'])
                monitor_stop.wait(1)
        monitor_thread = None
        try:
            self.stream.issue_stream_cmd(start_cmd)
            if any(list(self.device.get_rx_sensor_names(c)) for c in self.settings['channels']):
                monitor_thread = threading.Thread(target=monitor, daemon=True)
                monitor_thread.start()
            while not (cancel and cancel.is_set()):
                if hardware_samples and report['saved_samples_per_channel'] >= hardware_samples:
                    break
                elapsed = time.monotonic()-start
                if (finish is not None and time.monotonic() >= finish) or (finish is None and elapsed >= trigger_timeout):
                    break
                n = self.stream.recv(buffer, metadata, 0.25)
                error = metadata.error_code
                if error == self.uhd.types.RXMetadataErrorCode.timeout:
                    # A gated FPGA produces no packets while waiting for its event.
                    if self.trigger_mode == 'host' or report['triggered']:
                        report['timeouts'] += 1
                    continue
                if error == self.uhd.types.RXMetadataErrorCode.overflow:
                    report['overflows'] += 1
                    if len(report['discontinuities']) < 1000:
                        report['discontinuities'].append({'at_received_sample':report['received_samples_per_channel'], 'reason':'overflow'})
                    trigger.blocks.clear(); trigger.count = 0
                    continue
                if error != self.uhd.types.RXMetadataErrorCode.none:
                    report['other_errors'] += 1
                    raise RuntimeError('Receive error: '+str(error))
                if not n:
                    continue
                if report['first_sample_device_time'] is None:
                    report['first_sample_device_time'] = metadata.time_spec.get_real_secs()
                report['received_samples_per_channel'] += n
                block = buffer[:, :n]
                for i in range(len(clipped)):
                    peak = np.maximum(np.abs(block[i].real), np.abs(block[i].imag))
                    clipped[i] += int(np.count_nonzero(peak >= 32767/32768))
                    peaks[i] = max(peaks[i], float(peak.max()))
                accepted = trigger.accept(block, force=trigger_after_seconds is not None and elapsed >= trigger_after_seconds)
                if trigger.fired and finish is None:
                    report['triggered'] = True
                    report['trigger_received_sample'] = report['received_samples_per_channel']-n
                    report['pretrigger_samples_per_channel'] = sum(b.shape[1] for b in accepted[:-1])
                    report['trigger_sample_device_time'] = metadata.time_spec.get_real_secs()
                    report['first_saved_sample_device_time'] = metadata.time_spec.get_real_secs()-report['pretrigger_samples_per_channel']/self.rate
                    finish = time.monotonic()+duration+(1 if hardware_samples else 0)
                for saved in accepted:
                    if handles:
                        np.asarray(saved.T, dtype='<c8').tofile(handles)
                    report['saved_samples_per_channel'] += saved.shape[1]
                if on_spectrum and n >= bins and time.monotonic()-last_frame >= 0.1:
                    frame = spectrum(block[0], self.rate, self.settings['frequency_hz'], bins,
                                     self.calibration['level_correction_db'] if self.calibration else 0)
                    frame['time_utc'] = utc()
                    frame['telemetry'] = self.telemetry
                    frame['signal'] = [{'channel': c, 'clipped_samples': clipped[i],
                                        'peak_dbfs': 20*math.log10(max(peaks[i], 1e-12))}
                                       for i, c in enumerate(self.settings['channels'])]
                    on_spectrum(frame)
                    last_frame = time.monotonic()
            report['complete'] = report['triggered'] and not (
                report['overflows'] or report['other_errors'] or (cancel and cancel.is_set())) and (
                report['saved_samples_per_channel'] == hardware_samples if hardware_samples else
                finish is not None and time.monotonic() >= finish)
        except BaseException as error:
            report.update(complete=False, error=str(error))
            raise
        finally:
            try:
                try:
                    self.stream.issue_stream_cmd(self.uhd.types.StreamCMD(self.uhd.types.StreamMode.stop_cont))
                finally:
                    monitor_stop.set()
                    try:
                        if monitor_thread:
                            monitor_thread.join(timeout=5)
                            if monitor_thread.is_alive():
                                raise RuntimeError('Receiver telemetry did not stop')
                    finally:
                        if handles:
                            handles.close()
            except BaseException as error:
                report.update(complete=False, error=str(error))
                raise
            finally:
                report['telemetry'] = self.telemetry
                report['signal'] = [{'channel': c, 'clipped_samples': clipped[i],
                                     'peak_dbfs': 20*math.log10(max(peaks[i], 1e-12))}
                                    for i, c in enumerate(self.settings['channels'])]
                report['cancelled'] = bool(cancel and cancel.is_set())
                if report['cancelled']:
                    report['complete'] = False
                report['elapsed_seconds'] = time.monotonic()-start
                report['ended_at_utc'] = utc()
                if output:
                    atomic_json(output.with_suffix(output.suffix+'.json'), report)
        return report
