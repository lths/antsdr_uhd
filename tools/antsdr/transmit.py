"""Finite, timed transmit waveforms for the local E200 dashboard."""
import time

from .store import number, utc

DEFAULT = {'frequency_hz': 902050000, 'sample_rate': 1000000,
           'bandwidth_hz': 1000000, 'gain_db': 0, 'amplitude': 0.1,
           'duration': 0.05, 'tone_hz': 50000, 'waveform': 'tone',
           'channel': 0, 'start_delay_seconds': 0.1}


def settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT):
        raise ValueError('Unknown transmit setting')
    result = dict(DEFAULT, **value)
    for key, low, high in [('frequency_hz', 47e6, 6e9), ('sample_rate', 1e5, 10e6),
                           ('bandwidth_hz', 2e5, 20e6), ('gain_db', 0, 89.75),
                           ('amplitude', 1e-6, 1), ('duration', .001, 2),
                           ('start_delay_seconds', .1, 2)]:
        number(result[key], low, high, key)
    number(result['tone_hz'], 1, min(result['sample_rate'], result['bandwidth_hz']) / 2 * .9,
           'tone_hz')
    if result['waveform'] not in ('tone', 'two_tone', 'chirp'):
        raise ValueError('Unsupported transmit waveform')
    if type(result['channel']) is not int or result['channel'] not in (0, 1):
        raise ValueError('Transmit channel must be 0 or 1')
    if result['sample_rate'] * result['duration'] > 2000000:
        raise ValueError('A burst is limited to two million samples')
    return result


def waveform(config, rate, offset, count, total):
    import numpy as np
    index = np.arange(offset, offset + count, dtype=np.float64)
    phase = 2 * np.pi * config['tone_hz'] * index / rate
    if config['waveform'] == 'chirp':
        phase = 2 * np.pi * config['tone_hz'] * (-index / rate + index**2 / (rate * total))
    samples = np.exp(1j * phase)
    if config['waveform'] == 'two_tone':
        samples = (samples + np.exp(-1j * phase)) / 2
    # One envelope for the whole burst, independent of packet boundaries.
    ramp = max(1, min(round(rate * .001), total // 4))
    distance = np.minimum(index, total - 1 - index)
    envelope = np.sin(np.minimum(distance / ramp, 1) * np.pi / 2)**2
    return (config['amplitude'] * envelope * samples).astype(np.complex64)


class Transmitter:
    def __init__(self, address, config):
        import uhd
        self.uhd = uhd
        self.settings = settings(config)
        self.device = self.stream = None
        self.minimum_gain = None
        self.report = None
        try:
            self.device = uhd.usrp.MultiUSRP('type=ant,addr=' + address)
            channel = self.settings['channel']
            if channel >= self.device.get_tx_num_channels():
                raise ValueError('Transmit channel is unavailable')
            self.minimum_gain = self.device.get_tx_gain_range(channel).start()
            self.device.set_tx_gain(self.minimum_gain, channel)
            for key, getter in [('frequency_hz', 'get_tx_freq_range'),
                                ('bandwidth_hz', 'get_tx_bandwidth_range'),
                                ('gain_db', 'get_tx_gain_range')]:
                bounds = getattr(self.device, getter)(channel)
                if not bounds.start() <= self.settings[key] <= bounds.stop():
                    raise ValueError(key + ' exceeds device capabilities')
            self.device.set_tx_rate(self.settings['sample_rate'], channel)
            self.device.set_tx_freq(uhd.types.TuneRequest(self.settings['frequency_hz']), channel)
            self.device.set_tx_bandwidth(self.settings['bandwidth_hz'], channel)
            self.device.set_tx_antenna('TX/RX', channel)
            args = uhd.usrp.StreamArgs('fc32', 'sc16')
            args.channels = [channel]
            self.stream = self.device.get_tx_stream(args)
            self.device.set_time_now(uhd.types.TimeSpec(0))
            self.rate = self.device.get_tx_rate(channel)
            self.serial = self.device.get_usrp_tx_info(channel).get('mboard_serial')
        except BaseException:
            self.close()
            raise

    def run(self, cancel, progress=None):
        import numpy as np
        config = self.settings
        total = round(config['duration'] * self.rate)
        if not 1 <= total <= 2000000:
            raise ValueError('Actual sample rate exceeds burst sample limit')
        if config['tone_hz'] >= self.rate / 2:
            raise ValueError('Tone exceeds actual sample-rate Nyquist limit')
        report = {'schema_version': 1, 'started_at_utc': utc(), 'requested': config,
                  'actual_sample_rate': self.rate, 'device_serial': self.serial,
                  'samples_sent': 0, 'planned_samples': total, 'complete': False,
                  'cancelled': False, 'async_events': []}
        end_sent = acknowledged = False
        self.report = report
        try:
            self.device.set_tx_gain(config['gain_db'], config['channel'])
            report['actual_frequency_hz'] = self.device.get_tx_freq(config['channel'])
            report['actual_bandwidth_hz'] = self.device.get_tx_bandwidth(config['channel'])
            report['actual_gain_db'] = self.device.get_tx_gain(config['channel'])
            # Keep a long requested delay cancellable before queuing timed RF.
            if cancel.wait(max(0, config['start_delay_seconds'] - .1)):
                report['cancelled'] = True
                return report
            start = self.device.get_time_now().get_real_secs() + .1
            report['scheduled_device_time'] = start
            metadata = self.uhd.types.TXMetadata()
            while report['samples_sent'] < total and not cancel.is_set():
                offset = report['samples_sent']
                count = min(4096, total - offset)
                block = waveform(config, self.rate, offset, count, total)
                metadata.start_of_burst = metadata.has_time_spec = offset == 0
                metadata.end_of_burst = offset + count == total
                metadata.time_spec = self.uhd.types.TimeSpec(start)
                sent = self.stream.send(block, metadata, .5)
                report['samples_sent'] += sent
                if sent != count:
                    raise RuntimeError('Transmit send timed out before the complete burst')
                end_sent = metadata.end_of_burst
                if progress:
                    progress(report['samples_sent'], total)
            report['cancelled'] = cancel.is_set()
            if not end_sent and report['samples_sent']:
                metadata = self.uhd.types.TXMetadata()
                metadata.end_of_burst = True
                self.stream.send(np.empty(0, np.complex64), metadata, .5)
                end_sent = True
            deadline = time.monotonic() + 1 + total / self.rate
            event = self.uhd.types.TXAsyncMetadata()
            while not cancel.is_set() and time.monotonic() < deadline:
                if not self.stream.recv_async_msg(event, .05):
                    continue
                value = str(event.event_code)
                report['async_events'].append(value)
                if 'burst_ack' in value:
                    acknowledged = True
                    break
                raise RuntimeError('Transmit hardware error: ' + value)
            if not cancel.is_set() and not acknowledged:
                raise RuntimeError('Transmit burst acknowledgement timed out')
            report['cancelled'] = cancel.is_set()
            report['complete'] = report['samples_sent'] == total and acknowledged and not report['cancelled']
            return report
        except BaseException as error:
            report['error'] = str(error)
            raise
        finally:
            try:
                self.device.set_tx_gain(self.minimum_gain, config['channel'])
                if report['samples_sent'] and not end_sent:
                    metadata = self.uhd.types.TXMetadata()
                    metadata.end_of_burst = True
                    self.stream.send(np.empty(0, np.complex64), metadata, .5)
            except BaseException as error:
                report['complete'] = False
                report['error'] = str(error)
                raise
            finally:
                report['ended_at_utc'] = utc()

    def close(self):
        try:
            if self.device is not None and self.minimum_gain is not None:
                self.device.set_tx_gain(self.minimum_gain, self.settings['channel'])
        finally:
            self.stream = self.device = None
