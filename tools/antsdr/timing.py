"""PPS tracking and conservative GPS UTC association through the active owner."""
from collections import deque
from datetime import datetime, timezone
import threading
import time

from .gpsdo import snapshot as gps_snapshot


class Timing:
    def __init__(self, device, source, gps_path='.antsdr/timing/gpsdo.json'):
        self.device, self.source, self.gps_path = device, source, gps_path
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.edges = deque(maxlen=8)
        self.last = None
        self.last_edge_host = None
        self.intervals = deque(maxlen=60)
        self.missed = 0
        self.discontinuities = 0
        self.current_interval_good = False
        self.utc_losses = 0
        self.anchor = None
        self.candidate = None
        self.gps = {'available': False}
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                value = self.device.get_time_last_pps(0).get_real_secs()
                host = time.monotonic()
                gps = gps_snapshot(self.gps_path)
                with self.lock:
                    self.observe(value, host, gps)
                    self.error = None
            except Exception as error:
                with self.lock:
                    self.error = str(error)
                    if self.anchor is not None:
                        self.utc_losses += 1
                    self.anchor = None
            self.stop.wait(.05)

    def observe(self, value, host, gps):
        """The caller holds lock. Kept deterministic for association tests."""
        had_anchor = self.anchor is not None
        try:
            self._observe(value, host, gps)
        finally:
            if had_anchor and self.anchor is None:
                self.utc_losses += 1

    def _observe(self, value, host, gps):
        self.gps = gps
        if self.last is not None and value != self.last:
            interval = value - self.last
            self.current_interval_good = .999 <= interval <= 1.001
            if self.last_edge_host is None:
                pass  # First edge has no previously observed edge to compare.
            elif self.current_interval_good:
                self.intervals.append(interval)
            elif interval > 1.5:
                self.missed += max(1, round(interval) - 1)
                self.anchor = self.candidate = None
            else:
                self.discontinuities += 1
                self.anchor = self.candidate = None
            self.edges.append((value, host))
            self.last_edge_host = host
        self.last = value
        fresh = (gps.get('available', False) and gps.get('gps_locked', False)
                 and gps.get('pps_enabled', False) and gps.get('rmc_valid', False)
                 and -.06 <= host - gps.get('rmc_received_monotonic', -100) < 1.5)
        if (not fresh or self.source != 'external'
                or (self.last_edge_host is not None and host - self.last_edge_host > 1.5)):
            self.anchor = self.candidate = None
            return
        # The RMC sentence must arrive shortly after the matching PPS. Refuse
        # delayed/ambiguous sentences instead of guessing which UTC second fired.
        received = gps['rmc_received_monotonic']
        matches = [(value, when) for value, when in self.edges if -.06 <= received - when <= .7]
        if len(matches) != 1:
            return
        seconds = datetime.fromisoformat(gps['rmc_utc'].replace('Z', '+00:00')).timestamp()
        candidate = (matches[0][0], seconds)
        if self.candidate == candidate:
            return
        if self.candidate is not None:
            previous_device, previous_utc = self.candidate
            if seconds - previous_utc == 1 and .999 < candidate[0] - previous_device < 1.001:
                self.anchor = {'device_seconds': candidate[0], 'utc_seconds': seconds,
                               'associated_monotonic': host,
                               'method': 'two consecutive PPS/RMC epochs; RMC latency <= 0.7 s'}
            else:
                self.anchor = None
        self.candidate = candidate

    def status(self):
        with self.lock:
            now = time.monotonic()
            age = None if self.last_edge_host is None else now - self.last_edge_host
            healthy = age is not None and age < 1.5 and self.current_interval_good
            valid = bool(self.anchor and healthy
                         and now - self.anchor['associated_monotonic'] < 2.5
                         and not self.error)
            intervals = list(self.intervals)
            return {'time_source': self.source, 'pps_seen': bool(self.edges),
                    'pps_age_seconds': age, 'pps_healthy': healthy,
                    'missed_pulses': self.missed + (max(0, int(age)) if age is not None and age > 1.5 else 0),
                    'time_discontinuities': self.discontinuities, 'utc_losses': self.utc_losses,
                    'last_pps_device_seconds': self.last, 'observed_intervals': intervals,
                    'local_clock_offset_ppm': ((sum(intervals)/len(intervals)-1)*1e6)
                    if intervals and healthy and self.source == 'external'
                    and self.gps.get('available') and self.gps.get('gps_locked')
                    and self.gps.get('pps_enabled') else None,
                    'utc_valid': valid, 'utc_anchor': dict(self.anchor) if valid else None,
                    'gpsdo': dict(self.gps), 'error': self.error}

    def wait_utc(self, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status()['utc_valid']:
                return
            if self.stop.wait(.05):
                break
        raise RuntimeError('GPS UTC unavailable: require external PPS, GPS lock, and two unambiguous live RMC epochs')

    def utc(self, device_seconds):
        state = self.status()
        anchor = state['utc_anchor']
        if anchor is None or abs(device_seconds - anchor['device_seconds']) > 3:
            return None
        stamp = anchor['utc_seconds'] + device_seconds - anchor['device_seconds']
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace('+00:00', 'Z')

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError('PPS monitor did not stop')
        self.device = None
