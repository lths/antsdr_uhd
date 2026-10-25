"""Receive-only scanning using the AD9361's verified on-chip RX fast-lock RAM."""
import time

import numpy as np

from .radio import Receiver, spectrum
from .store import atomic_json, number, utc


BASE = '/mboards/0/rx_fastlock/'


def validate(frequencies, dwell, cycles, settings):
    if not isinstance(frequencies, list) or not 1 <= len(frequencies) <= 8:
        raise ValueError('Fast-lock scanning requires 1–8 RX frequencies')
    for frequency in frequencies:
        number(frequency, 70e6, 6e9, 'frequency_hz')
    if max(frequencies) - min(frequencies) > 100e6:
        raise ValueError('Fast-lock scan span must fit within a 100 MHz calibration window')
    number(dwell, .01, 1, 'dwell_seconds')
    if type(cycles) is not int or not 1 <= cycles <= 128:
        raise ValueError('cycles must be an integer within 1–128')
    if len(settings['channels']) != 1 or settings.get('lo_offset_hz', 0):
        raise ValueError('Fast-lock scanning requires one RX channel and zero LO offset')
    if settings['sample_rate'] * dwell > 2e6:
        raise ValueError('Scan dwell is limited to two million samples per hop')


def run(address, settings, frequencies, dwell=.025, cycles=1, report=None,
        cancel=None, on_spectrum=None, calibration=None):
    validate(frequencies, dwell, cycles, settings)
    result = {'kind': 'fastlock-scan', 'complete': False, 'started_at_utc': utc(),
              'transmitted_samples': 0, 'hops': [], 'preloaded_profiles': [],
              'requested_frequencies_hz': frequencies,
              'timing_note': 'SPI/host scheduled; no deterministic FPGA hop cadence is claimed'}
    radio = None
    tree = None
    try:
        radio = Receiver(address, dict(settings, frequency_hz=frequencies[0]), calibration)
        device, stream, uhd = radio.device, radio.stream, radio.uhd
        tree = device.get_tree()
        if not tree.exists(BASE+'store'):
            raise ValueError('Rebuild the host driver with RX fast-lock support')
        channel = settings['channels'][0]
        correction = 1 + calibration['frequency_error_ppm']/1e6 if calibration else 1
        for slot, frequency in enumerate(frequencies):
            preparation_start = time.monotonic()
            device.set_rx_freq(uhd.types.TuneRequest(frequency / correction), channel)
            deadline = time.monotonic() + 2
            while not device.get_rx_sensor('lo_locked', channel).to_bool():
                if time.monotonic() >= deadline:
                    raise RuntimeError('LO failed to lock during profile preparation')
                time.sleep(.001)
            tree.access_int(BASE+'store').set(slot)
            result['preloaded_profiles'].append({'slot': slot, 'requested_frequency_hz': frequency,
                                                'preparation_seconds': time.monotonic()-preparation_start,
                                                'actual_frequency_hz': device.get_rx_freq(channel)})
        count = round(radio.rate * dwell)
        buffer = np.empty((1, 16384), dtype=np.complex64)
        for cycle in range(cycles):
            for slot, frequency in enumerate(frequencies):
                if cancel and cancel.is_set():
                    result['cancelled'] = True
                    return result
                start = time.monotonic()
                tree.access_int(BASE+'recall').set(slot)
                recall_seconds = time.monotonic() - start
                time.sleep(.002)  # Allow analog tracking to settle before sampling.
                command = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
                command.num_samps, command.stream_now = count, True
                stream.issue_stream_cmd(command)
                received, first, tail, energy = 0, None, np.empty(0, np.complex64), 0.
                deadline = time.monotonic() + dwell + 2
                metadata = uhd.types.RXMetadata()
                while received < count:
                    if cancel and cancel.is_set():
                        result['cancelled'] = True
                        return result
                    size = stream.recv(buffer, metadata, .25)
                    if metadata.error_code == uhd.types.RXMetadataErrorCode.timeout:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Fast-lock dwell timed out')
                        continue
                    if metadata.error_code != uhd.types.RXMetadataErrorCode.none:
                        raise RuntimeError('Fast-lock receive error: ' + str(metadata.error_code))
                    if not size:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Empty fast-lock receive dwell')
                        continue
                    if size > count - received:
                        raise RuntimeError('Fast-lock capture exceeded finite sample count')
                    if first is None:
                        first = metadata.time_spec.get_real_secs()
                    received += size
                    block = buffer[0, :size]
                    energy += float(np.vdot(block, block).real)
                    tail = np.concatenate((tail, block))[-1024:]
                actual = device.get_rx_freq(channel)
                frame = spectrum(tail, radio.rate, actual, bins=1024)
                frame.update(time_utc=utc(), timing=radio.timing.status())
                entry = {'cycle': cycle, 'slot': slot, 'frequency_hz': actual,
                         'requested_frequency_hz': frequency, 'recall_seconds': recall_seconds,
                         'analog_settle_seconds': .002,
                         'samples': received, 'first_sample_device_time': first,
                         'first_sample_utc': radio.timing.utc(first),
                         'rms_dbfs': float(10*np.log10(max(energy/received, 1e-24))),
                         'spectrum': frame}
                result['hops'].append(entry)
                if on_spectrum:
                    on_spectrum(frame)
                if report:
                    atomic_json(report, result)
        result['complete'] = True
    except BaseException as error:
        result['error'] = str(error)
        raise
    finally:
        try:
            if radio:
                try:
                    radio.stream.issue_stream_cmd(radio.uhd.types.StreamCMD(radio.uhd.types.StreamMode.stop_cont))
                finally:
                    try:
                        if tree is not None and tree.exists(BASE+'disable'):
                            tree.access_bool(BASE+'disable').set(True)
                    finally:
                        radio.close()
        except BaseException as error:
            result.update(complete=False, cleanup_error=str(error))
            raise
        finally:
            result['ended_at_utc'] = utc()
            if report:
                atomic_json(report, result)
    return result
