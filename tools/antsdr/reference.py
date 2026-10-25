"""Receive-only external 10 MHz lock/loss/reacquisition verification."""
import time

from .store import atomic_json, number, utc


def check(address, report, timeout=60):
    import numpy as np
    import uhd
    number(timeout, 1, 120, 'timeout')
    result = {'kind': 'reference-check', 'complete': False, 'transmitted_samples': 0,
              'started_at_utc': utc(), 'observations': [],
              'loss_test': 'Route the FPGA reference mux to none, then restore the 10 MHz input',
              'measurement_limit': 'Lock sensor and receive continuity; absolute RF frequency/power accuracy is not measured'}
    device = stream = None
    def wait_lock(expected, limit):
        deadline = time.monotonic() + limit
        stable = 0
        while time.monotonic() < deadline:
            locked = device.get_mboard_sensor('ref_locked', 0).to_bool()
            result['observations'].append({'monotonic': time.monotonic(), 'locked': locked, 'expected': expected})
            stable = stable + 1 if locked == expected else 0
            if stable >= 3:
                return True
            time.sleep(.2)
        return False
    try:
        device = uhd.usrp.MultiUSRP('type=ant,addr='+address)
        result['device_serial'] = device.get_usrp_rx_info(0).get('mboard_serial')
        if 'ref_locked' not in device.get_mboard_sensor_names(0):
            raise RuntimeError('Reference lock sensor unavailable')
        device.set_rx_rate(1e6, 0)
        device.set_rx_freq(uhd.types.TuneRequest(902050000), 0)
        device.set_rx_gain(0, 0)
        args = uhd.usrp.StreamArgs('fc32', 'sc16')
        args.channels = [0]
        stream = device.get_rx_stream(args)
        device.set_time_source('internal', 0)
        device.set_clock_source('external', 0)
        result['acquired'] = wait_lock(True, timeout)
        if not result['acquired']:
            result['error'] = 'External 10 MHz reference failed to lock'
            return result
        command = uhd.types.StreamCMD(uhd.types.StreamMode.num_done)
        command.num_samps, command.stream_now = 25000, True
        stream.issue_stream_cmd(command)
        received = 0
        buffer = np.empty((1, min(16384, stream.get_max_num_samps())), np.complex64)
        metadata = uhd.types.RXMetadata()
        deadline = time.monotonic()+3
        while received < 25000:
            count = stream.recv(buffer, metadata, .25)
            if metadata.error_code != uhd.types.RXMetadataErrorCode.none:
                raise RuntimeError('Reference receive check failed: '+str(metadata.error_code))
            received += count
            if time.monotonic() > deadline:
                raise TimeoutError('Reference receive check timed out')
        result['received_samples'] = received
        device.set_time_source('none', 0)
        result['loss_detected'] = wait_lock(False, 3)
        device.set_time_source('internal', 0)
        result['reacquired'] = wait_lock(True, timeout)
        result['complete'] = received == 25000 and result['loss_detected'] and result['reacquired']
    except BaseException as error:
        result['error'] = str(error)
        raise
    finally:
        try:
            if stream:
                stream.issue_stream_cmd(uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont))
            if device:
                device.set_clock_source('internal', 0)
                device.set_time_source('internal', 0)
        except BaseException as error:
            result.update(complete=False, cleanup_error=str(error))
            raise
        finally:
            stream = device = None
            result['ended_at_utc'] = utc()
            atomic_json(report, result)
    return result
