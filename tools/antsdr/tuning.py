"""AD9361 receiver controls through the active UHD owner, without another session."""
import time


def channel_settings(settings, channel):
    return dict(settings, **settings.get('channel_settings', {}).get(str(channel), {}))


def frontend(channel):
    return '/mboards/0/dboards/A/rx_frontends/' + ('A' if channel == 0 else 'B')


def configure(device, uhd, settings, frequency):
    channels = settings['channels']
    # Tracking and analog bandwidth are shared by both receive chains.
    device.set_rx_dc_offset(settings.get('auto_dc_offset', True), channels[0])
    device.set_rx_iq_balance(settings.get('auto_iq_balance', True), channels[0])
    for channel in channels:
        device.set_rx_agc(False, channel)
    for channel in channels:
        config = channel_settings(settings, channel)
        gain = config['gain_db']
        bounds = device.get_rx_gain_range(channel)
        if not bounds.start() <= gain <= bounds.stop():
            raise ValueError('Gain exceeds connected radio capabilities')
        offset = settings.get('lo_offset_hz', 0)
        request = uhd.types.TuneRequest(frequency, offset) if offset else uhd.types.TuneRequest(frequency)
        device.set_rx_freq(request, channel)
        device.set_rx_bandwidth(settings['bandwidth_hz'], channel)
        device.set_rx_gain(gain, channel)
        device.set_rx_antenna(config.get('antenna', 'RX2'), channel)
        mode = config.get('gain_mode', 'manual')
        if mode != 'manual':
            path = frontend(channel) + '/gain/agc/mode/value'
            tree = device.get_tree()
            if not tree.exists(path):
                raise ValueError('This driver does not expose selectable AGC modes')
            tree.access_str(path).set(mode)
            device.set_rx_agc(True, channel)
    # Reject incompatible custom filters before writing any taps.
    filters = []
    if 'fir_taps' in settings:
        for channel in channels:
            names = [n for n in device.get_rx_filter_names(channel) if n.endswith('FIR_1')]
            if len(names) != 1:
                raise ValueError('RX FIR filter is unavailable')
            name = names[0]
            value = device.get_rx_filter(name, channel)
            if value.is_bypassed() or len(value.get_taps()) != len(settings['fir_taps']):
                raise ValueError('Custom FIR taps must match the active filter tap count')
            filters.append((channel, name, value))
        for channel, name, value in filters:
            value.set_taps(settings['fir_taps'])
            device.set_rx_filter(name, value, channel)
            if list(device.get_rx_filter(name, channel).get_taps()) != settings['fir_taps']:
                raise RuntimeError('RX FIR coefficient verification failed')
    deadline = time.monotonic() + 2
    for channel in channels:
        if 'lo_locked' in device.get_rx_sensor_names(channel):
            while not device.get_rx_sensor('lo_locked', channel).to_bool():
                if time.monotonic() >= deadline:
                    raise RuntimeError('RX LO failed to lock within two seconds')
                time.sleep(.01)
    time.sleep(settings.get('settle_seconds', .05))


def telemetry(device, channels):
    result = {'updated_monotonic': time.monotonic(), 'channels': [], 'errors': []}
    try:
        if 'ref_locked' in device.get_mboard_sensor_names(0):
            result['reference_locked'] = device.get_mboard_sensor('ref_locked', 0).to_bool()
    except Exception as error:
        result['errors'].append({'channel': 'board', 'error': str(error)})
    for channel in channels:
        item = {'channel': channel}
        try:
            names = device.get_rx_sensor_names(channel)
            for name in ('rssi', 'temp', 'lo_locked', 'gain_index', 'agc_mode', 'agc_state', 'overload'):
                if name not in names:
                    continue
                sensor = device.get_rx_sensor(name, channel)
                item[name] = sensor.to_bool() if name == 'lo_locked' else sensor.to_real()
            if 'agc_mode' in item:
                item['gain_mode'] = {0: 'manual', 1: 'fast', 2: 'slow', 3: 'hybrid'}[int(item['agc_mode'])]
            if 'overload' in item:
                bits = int(item['overload'])
                item['overload_flags'] = {key: bool(bits & mask) for key, mask in (
                    ('digital_saturation', 1), ('small_adc', 2), ('large_adc', 4),
                    ('small_lmt', 8), ('large_lmt', 16), ('low_power', 32), ('gain_locked', 64))}
            # Keep legacy UHD gain units explicit: the driver uses a 0..76
            # table-index scale, not a calibrated frequency-dependent RF gain.
            item['gain_setting'] = float(device.get_rx_gain(channel))
            tree = device.get_tree()
            base = frontend(channel)
            for field, path in (('agc_enabled', '/gain/agc/enable'),
                                ('auto_dc_offset', '/dc_offset/enable'),
                                ('auto_iq_balance', '/iq_balance/enable')):
                if tree.exists(base + path):
                    item[field] = bool(tree.access_bool(base + path).get())
        except Exception as error:
            result['errors'].append({'channel': channel, 'error': str(error)})
        result['channels'].append(item)
    return result


def capabilities(device, channels):
    result = {'rf_chip': 'AD9361', 'identification': 'user-confirmed hardware variant',
              'shared': ['rx_lo', 'analog_bandwidth', 'tracking', 'master_clock'],
              'gain_modes': ['manual', 'slow', 'fast'],
              'gain_units': 'legacy UHD 0..76 gain-table-index scale',
              'rssi_units': 'relative dB; absolute dBm requires bench calibration',
              'channels': [], 'errors': []}
    for channel in channels:
        item = {'channel': channel}
        try:
            for key, getter in (('frequency_hz', 'get_rx_freq_range'),
                                ('gain_setting', 'get_rx_gain_range'),
                                ('bandwidth_hz', 'get_rx_bandwidth_range')):
                bounds = getattr(device, getter)(channel)
                item[key] = {'min': bounds.start(), 'max': bounds.stop()}
            item['antennas'] = list(device.get_rx_antennas(channel))
            item['sensors'] = list(device.get_rx_sensor_names(channel))
            item['filters'] = []
            for name in device.get_rx_filter_names(channel):
                value = device.get_rx_filter(name, channel)
                entry = {'name': name, 'bypassed': value.is_bypassed(), 'description': str(value)}
                if hasattr(value, 'get_taps'):
                    entry.update(taps=list(value.get_taps()), input_rate=value.get_input_rate(),
                                 output_rate=value.get_output_rate())
                item['filters'].append(entry)
        except Exception as error:
            result['errors'].append({'channel': channel, 'error': str(error)})
        result['channels'].append(item)
    return result
