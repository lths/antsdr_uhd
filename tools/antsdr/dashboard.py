"""Loopback dashboard with one exclusive RX/TX job and read-only health."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from . import ops
from .radio import Receiver
from .store import atomic_json, name, number, utc
from .transmit import Transmitter, settings as transmit_settings
from .gpsdo import snapshot as gps_snapshot


class Application:
    def __init__(self, store, host, connect):
        self.store, self.host, self.connect = store, host, connect
        self.lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.cancel = threading.Event()
        self.thread = None
        self.state = {'job':None, 'frame':None, 'health':None}
        self.closed = threading.Event()

    def monitor(self):
        while not self.closed.is_set():
            try:
                with self.connect() as connection:
                    result = ops.health(connection)
                # Include bounded fault history, without exposing credential files.
            except Exception as error:
                result = {'healthy':False, 'error':str(error)}
            with self.lock:
                self.state['health'] = result
            self.closed.wait(3)

    def start(self, request):
        if not isinstance(request, dict):
            raise ValueError('A radio job must be a JSON object')
        if request.get('kind') == 'transmit':
            return self.start_transmit(request)
        if request.get('kind') in ('scan', 'throughput', 'soak'):
            return self.start_measurement(request)
        if set(request)-{'kind','profile','duration','threshold_dbfs','pretrigger_seconds','trigger_timeout','trigger_after_seconds','capture_name','trigger_mode','device_delay_seconds','sigmf'}:
            raise ValueError('Unknown job field')
        if type(request.get('sigmf', False)) is not bool:
            raise ValueError('sigmf must be a boolean')
        kind = request.get('kind')
        if kind not in ('spectrum', 'capture', 'inspect'):
            raise ValueError('Unsupported receive job')
        settings = self.store.get('profiles', request['profile'])
        duration = number(request.get('duration', 10), 0.01, 3600, 'duration')
        pretrigger = number(request.get('pretrigger_seconds', 0), 0, 5, 'pretrigger_seconds')
        trigger_timeout = number(request.get('trigger_timeout', 10), 0.1, 3600, 'trigger_timeout')
        threshold = request.get('threshold_dbfs')
        trigger_mode = request.get('trigger_mode', 'host')
        device_delay = request.get('device_delay_seconds')
        if trigger_mode not in ('host', 'device-time', 'fpga-level', 'external-edge'):
            raise ValueError('Unsupported trigger mode')
        if trigger_mode != 'host' and (pretrigger or request.get('trigger_after_seconds') is not None):
            raise ValueError('Hardware triggering cannot use host delay or pretrigger history')
        if trigger_mode == 'fpga-level':
            number(threshold, -90, 0, 'threshold_dbfs')
            if len(settings['channels']) != 1:
                raise ValueError('FPGA level triggering currently requires one RX channel')
        elif trigger_mode != 'host' and threshold is not None:
            raise ValueError('Threshold requires a level trigger')
        if trigger_mode == 'device-time':
            number(device_delay, .1, trigger_timeout, 'device_delay_seconds')
        elif device_delay is not None:
            raise ValueError('Device delay requires device-time triggering')
        trigger_after = request.get('trigger_after_seconds')
        if trigger_after is not None:
            number(trigger_after,0,trigger_timeout,'trigger_after_seconds')
            if threshold is not None: raise ValueError('Choose one trigger mode')
        if threshold is not None:
            number(threshold, -160, 0, 'threshold_dbfs')
        output = None
        if kind == 'capture':
            capture = name(request.get('capture_name', 'capture-'+str(time.time_ns())))
            output = self.store.root/'captures'/(capture+'.cf32')
        calibration = self.store.get('calibrations', settings['calibration']) if settings.get('calibration') else None
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('A radio job is already running')
            self.cancel.clear()
            self.state['frame'] = None
            self.state['job'] = {'kind':kind, 'status':'starting', 'started_at_utc':utc()}

            def frame(value):
                with self.lock:
                    self.state['frame'] = value
                    if 'stream_stats' in value:
                        self.state['job']['progress'] = value['stream_stats']

            def worker():
                receiver = None
                try:
                    receiver = Receiver(self.host, settings, calibration, trigger_mode=trigger_mode,
                                        hardware_threshold_dbfs=threshold if trigger_mode == 'fpga-level' else None)
                    with self.lock:
                        self.state['job']['status'] = 'receiving'
                    result = receiver.describe() if kind == 'inspect' else receiver.run(duration, output=output, threshold_dbfs=threshold if trigger_mode == 'host' else None,
                                          pretrigger_seconds=pretrigger, trigger_timeout=trigger_timeout,
                                          on_spectrum=frame, cancel=self.cancel, trigger_after_seconds=trigger_after,
                                          device_delay_seconds=device_delay)
                    receiver.close()
                    receiver = None
                    if output and request.get('sigmf') and result['saved_samples_per_channel']:
                        from .recordings import export_sigmf
                        result['sigmf'] = export_sigmf(output, output.with_suffix(''))
                    with self.lock:
                        self.state['job'].update(status='complete' if result['complete'] else 'stopped', report=result)
                        if output: self.state['job']['capture_file'] = output.name
                except Exception as error:
                    with self.lock:
                        self.state['job'].update(status='failed', error=str(error))
                finally:
                    if receiver:
                        receiver.close()
            self.thread = threading.Thread(target=worker, daemon=True)
            self.thread.start()

    def start_measurement(self, request):
        kind = request['kind']
        allowed = {'kind', 'profile'} | {
            'scan': {'frequencies', 'dwell', 'cycles'},
            'throughput': {'duration', 'rates', 'all_channels'},
            'soak': {'duration'}}[kind]
        if set(request) - allowed:
            raise ValueError('Unknown measurement field')
        config = self.store.get('profiles', request['profile'])
        duration = number(request.get('duration', 5), .1 if kind == 'throughput' else 1, 3600, 'duration')
        if kind == 'scan':
            from .scan import validate
            validate(request.get('frequencies'), request.get('dwell', .025), request.get('cycles', 1), config)
        rates = request.get('rates', [1e6, 5e6, 10e6])
        if kind == 'throughput':
            if not isinstance(rates, list) or not 1 <= len(rates) <= 32:
                raise ValueError('Choose 1–32 rates')
            for rate in rates:
                number(rate, 1e3, 61.44e6, 'rate')
            if type(request.get('all_channels', False)) is not bool:
                raise ValueError('all_channels must be a boolean')
        destination = self.store.root/'measurements'/(kind+'-'+str(time.time_ns())+'.json')
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('A radio job is already running')
            self.cancel.clear()
            self.state['frame'] = None
            self.state['job'] = {'kind': kind, 'status': 'starting', 'report_file': destination.name}
            def frame(value):
                with self.lock:
                    self.state['frame'] = value
                    if 'stream_stats' in value:
                        self.state['job']['progress'] = value['stream_stats']
            def completed_case(value):
                with self.lock:
                    self.state['job']['progress'] = {key: value.get(key) for key in
                                                   ('requested_rate', 'channels', 'passed', 'error')}
            def worker():
                try:
                    with self.lock:
                        self.state['job']['status'] = 'receiving'
                    if kind == 'scan':
                        from .scan import run
                        calibration = self.store.get('calibrations', config['calibration']) if config.get('calibration') else None
                        result = run(self.host, config, request['frequencies'], request.get('dwell', .025),
                                     request.get('cycles', 1), destination, self.cancel, frame, calibration)
                    elif kind == 'throughput':
                        from .measurements import throughput
                        result = throughput(self.host, config, rates, duration,
                                            [[0], [1], [0,1]] if request.get('all_channels') else None,
                                            destination, self.cancel, completed_case)
                    else:
                        from .measurements import endurance
                        result = endurance(self.host, config, duration, destination, self.cancel, on_progress=frame)
                    with self.lock:
                        self.state['job'].update(status='complete' if result['complete'] else 'stopped', report=result)
                except Exception as error:
                    with self.lock:
                        self.state['job'].update(status='failed', error=str(error))
            self.thread = threading.Thread(target=worker, daemon=True)
            self.thread.start()

    def start_transmit(self, request):
        if set(request) - {'kind', 'settings'}:
            raise ValueError('Unknown transmit job field')
        config = transmit_settings(request.get('settings', {}))
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('A radio job is already running')
            self.cancel.clear()
            self.state['frame'] = None
            self.state['job'] = {'kind': 'transmit', 'status': 'starting',
                                 'started_at_utc': utc(), 'settings': config}

            def progress(sent, total):
                with self.lock:
                    self.state['job'].update(status='transmitting', samples_sent=sent,
                                             planned_samples=total)

            def worker():
                transmitter = None
                destination = self.store.root/'transmit'/('burst-'+str(time.time_ns())+'.json')
                try:
                    transmitter = Transmitter(self.host, config)
                    with self.lock:
                        self.state['job']['status'] = 'scheduled'
                    result = transmitter.run(self.cancel, progress)
                    transmitter.close()
                    transmitter = None
                    atomic_json(destination, result)
                    with self.lock:
                        self.state['job'].update(status='complete' if result['complete'] else 'stopped',
                                                 report=result, report_file=destination.name)
                except Exception as error:
                    failed = transmitter.report if transmitter else None
                    if failed:
                        atomic_json(destination, failed)
                    with self.lock:
                        self.state['job'].update(status='failed', error=str(error))
                        if failed:
                            self.state['job'].update(report=failed, report_file=destination.name)
                finally:
                    if transmitter:
                        transmitter.close()
            self.thread = threading.Thread(target=worker, daemon=True)
            self.thread.start()

    def stop(self):
        self.cancel.set()

    def snapshot(self):
        with self.lock:
            result = json.loads(json.dumps(self.state))
        result['gpsdo'] = gps_snapshot(self.store.root/'timing/gpsdo.json')
        return result


def make_server(app, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type='application/json'):
            if not isinstance(body, bytes):
                body = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get('Host') in ('127.0.0.1:'+str(self.server.server_port), 'localhost:'+str(self.server.server_port))

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {'error':'Invalid host'})
            path = urlsplit(self.path).path
            if path in ('/', '/app.js', '/style.css'):
                filename = {'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[path]
                kind = {'/':'text/html; charset=utf-8','/app.js':'application/javascript','/style.css':'text/css'}[path]
                return self.send(200, (Path(__file__).parent/'web'/filename).read_bytes(), kind)
            if path == '/api/session':
                return self.send(200, {'token':app.token})
            if path == '/api/state':
                return self.send(200, app.snapshot())
            if path == '/api/records':
                return self.send(200, {kind:app.store.list(kind) for kind in ('profiles','calibrations')})
            return self.send(404, {'error':'Not found'})

        def do_POST(self):
            if not self.allowed_host() or not secrets.compare_digest(self.headers.get('X-AntSDR-Token',''), app.token):
                return self.send(403, {'error':'Invalid session token'})
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://127.0.0.1:'+str(self.server.server_port), 'http://localhost:'+str(self.server.server_port)):
                return self.send(403, {'error':'Invalid origin'})
            if self.headers.get('Content-Type') != 'application/json':
                return self.send(415, {'error':'Use application/json'})
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 16384:
                    raise ValueError('Invalid body size')
                request = json.loads(self.rfile.read(size))
                path = urlsplit(self.path).path
                if path == '/api/jobs': app.start(request)
                elif path == '/api/stop': app.stop()
                elif path in ('/api/profiles','/api/calibrations'):
                    app.store.put(path.rsplit('/',1)[1], request)
                else: return self.send(404, {'error':'Not found'})
                return self.send(200, {'ok':True})
            except (ValueError, KeyError, TypeError, FileNotFoundError) as error:
                return self.send(400, {'error':str(error)})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def serve(app, port):
    server = make_server(app, port)
    threading.Thread(target=app.monitor, daemon=True).start()
    print('ANTSDR dashboard: http://127.0.0.1:'+str(port), flush=True)
    try:
        server.serve_forever()
    finally:
        app.closed.set()
        app.stop()
        if app.thread: app.thread.join(timeout=5)
        server.server_close()
