"""Receive only: bounded streaming, triggered capture and calibrated spectra."""
from collections import deque
import json
import math
from pathlib import Path
import threading
import time

from .store import atomic_json, number, profile, utc


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
    def __init__(self, address, settings, calibration=None):
        import uhd
        self.uhd = uhd
        self.settings = profile(settings)
        self.calibration = calibration
        self.device = uhd.usrp.MultiUSRP('type=ant,addr='+address)
        self.serial = self.device.get_usrp_rx_info(self.settings['channels'][0]).get('mboard_serial', '')
        if calibration and calibration['device_serial'].upper() != self.serial.upper():
            raise ValueError('Calibration serial does not match the connected radio')
        self.device.set_clock_source(self.settings.get('clock_source', 'internal'))
        ppm = calibration['frequency_error_ppm'] if calibration else 0
        tune = self.settings['frequency_hz']/(1+ppm/1e6)
        for channel in self.settings['channels']:
            if channel >= self.device.get_rx_num_channels():
                raise ValueError('Requested channel is unavailable')
            for value, bounds, field in (
                    (tune, self.device.get_rx_freq_range(channel), 'frequency'),
                    (self.settings['gain_db'], self.device.get_rx_gain_range(channel), 'gain'),
                    (self.settings['bandwidth_hz'], self.device.get_rx_bandwidth_range(channel), 'bandwidth')):
                if not bounds.start() <= value <= bounds.stop():
                    raise ValueError('{} exceeds the connected radio capabilities'.format(field))
            self.device.set_rx_rate(self.settings['sample_rate'], channel)
            self.device.set_rx_freq(uhd.types.TuneRequest(tune), channel)
            self.device.set_rx_gain(self.settings['gain_db'], channel)
            self.device.set_rx_bandwidth(self.settings['bandwidth_hz'], channel)
            self.device.set_rx_antenna(self.settings.get('antenna', 'RX2'), channel)
        self.rate = self.device.get_rx_rate(self.settings['channels'][0])
        stream_args = uhd.usrp.StreamArgs('fc32', 'sc16')
        stream_args.channels = self.settings['channels']
        self.stream = self.device.get_rx_stream(stream_args)
        self.actual = [{'channel': c, 'frequency_hz': self.device.get_rx_freq(c),
                        'sample_rate': self.device.get_rx_rate(c),
                        'gain_db': self.device.get_rx_gain(c),
                        'bandwidth_hz': self.device.get_rx_bandwidth(c)} for c in self.settings['channels']]

    def close(self):
        self.stream = None
        self.device = None

    def run(self, duration, output=None, threshold_dbfs=None, pretrigger_seconds=0,
            trigger_timeout=10, on_spectrum=None, cancel=None, bins=1024, trigger_after_seconds=None):
        import numpy as np
        number(duration, 0.01, 3600, 'duration')
        number(trigger_timeout, 0.1, 3600, 'trigger_timeout')
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
        start_cmd = self.uhd.types.StreamCMD(self.uhd.types.StreamMode.start_cont)
        start_cmd.stream_now = len(self.settings['channels']) == 1
        if not start_cmd.stream_now:
            start_cmd.time_spec = self.uhd.types.TimeSpec(self.device.get_time_now().get_real_secs()+0.1)
        report = {'schema_version':1, 'started_at_utc':utc(), 'device_serial':self.serial,
                  'requested':self.settings, 'actual':self.actual, 'calibration':self.calibration,
                  'format':'little-endian complex float32, sample-major interleaved channels',
                  'received_samples_per_channel':0, 'saved_samples_per_channel':0,
                  'overflows':0, 'timeouts':0, 'other_errors':0, 'triggered':False,
                  'first_sample_device_time':None, 'complete':False, 'discontinuities':[]}
        report['trigger_kind'] = 'host-delay' if trigger_after_seconds is not None else 'level' if threshold_dbfs is not None else 'immediate'
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
        try:
            self.stream.issue_stream_cmd(start_cmd)
            while not (cancel and cancel.is_set()):
                elapsed = time.monotonic()-start
                if (finish is not None and time.monotonic() >= finish) or (finish is None and elapsed >= trigger_timeout):
                    break
                n = self.stream.recv(buffer, metadata, 0.25)
                error = metadata.error_code
                if error == self.uhd.types.RXMetadataErrorCode.timeout:
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
                accepted = trigger.accept(block, force=trigger_after_seconds is not None and elapsed >= trigger_after_seconds)
                if trigger.fired and finish is None:
                    report['triggered'] = True
                    report['trigger_received_sample'] = report['received_samples_per_channel']-n
                    report['pretrigger_samples_per_channel'] = sum(b.shape[1] for b in accepted[:-1])
                    report['trigger_sample_device_time'] = metadata.time_spec.get_real_secs()
                    report['first_saved_sample_device_time'] = metadata.time_spec.get_real_secs()-report['pretrigger_samples_per_channel']/self.rate
                    finish = time.monotonic()+duration
                for saved in accepted:
                    if handles:
                        np.asarray(saved.T, dtype='<c8').tofile(handles)
                    report['saved_samples_per_channel'] += saved.shape[1]
                if on_spectrum and n >= bins and time.monotonic()-last_frame >= 0.1:
                    frame = spectrum(block[0], self.rate, self.settings['frequency_hz'], bins,
                                     self.calibration['level_correction_db'] if self.calibration else 0)
                    frame['time_utc'] = utc()
                    on_spectrum(frame)
                    last_frame = time.monotonic()
            report['complete'] = report['triggered'] and (finish is not None and time.monotonic() >= finish)
        finally:
            try:
                self.stream.issue_stream_cmd(self.uhd.types.StreamCMD(self.uhd.types.StreamMode.stop_cont))
            finally:
                if handles:
                    handles.close()
                report['elapsed_seconds'] = time.monotonic()-start
                report['ended_at_utc'] = utc()
                if output:
                    atomic_json(output.with_suffix(output.suffix+'.json'), report)
        return report
