import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zlib

from antsdr import ops
from antsdr.__main__ import DEFAULT
from antsdr.dashboard import Application, make_server
from antsdr.radio import Receiver, Trigger, spectrum
from antsdr.store import Store, profile, calibration


class RecordsTest(unittest.TestCase):
    def test_profile_rejects_invalid_and_unexpected_values(self):
        for key,value in [('name','../escape'),('frequency_hz',float('nan')),
                          ('gain_db',True),('channels',[0,0]),('channels',[True]),
                          ('clock_source','invalid'),('surprise','value')]:
            with self.subTest(key=key,value=value), self.assertRaises((ValueError,KeyError)):
                profile(dict(DEFAULT,**{key:value}))
        self.assertEqual(profile(DEFAULT),DEFAULT)

    def test_calibration_preserves_measurement_provenance(self):
        record={'schema_version':1,'name':'measured','device_serial':'0123456789abcdef',
                'measured_at_utc':'2026-10-22T00:00:00Z','reference':'Frequency counter and signal generator',
                'frequency_error_ppm':1.2,'level_correction_db':-0.5}
        self.assertEqual(calibration(record),record)
        for change in ({'reference':''},{'frequency_error_ppm':float('inf')},
                       {'measured_at_utc':'2026-10-22'},{'device_serial':'unknown'}):
            with self.assertRaises(ValueError): calibration(dict(record,**change))

    def test_store_round_trip_and_private_atomic_files(self):
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);store.put('profiles',DEFAULT)
            self.assertEqual(store.get('profiles','e200-rx'),DEFAULT)
            self.assertEqual(len(store.list('profiles')),1)
            path=Path(root)/'profiles/e200-rx.json'
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with self.assertRaises(ValueError): store.get('profiles','../escape')
            with self.assertRaises(ValueError): store.put('profiles',dict(DEFAULT,sample_rate=-1))
            self.assertEqual(store.get('profiles','e200-rx'),DEFAULT)


class SignalTest(unittest.TestCase):
    def test_dual_channel_time_sync_follows_final_stream_activation(self):
        fake=MagicMock()
        device=fake.usrp.MultiUSRP.return_value
        device.get_usrp_rx_info.return_value={'mboard_serial':'0123456789abcdef'}
        device.get_rx_num_channels.return_value=2
        device.get_rx_rate.return_value=1e6
        for getter in ('get_rx_freq_range','get_rx_gain_range','get_rx_bandwidth_range'):
            getattr(device,getter).return_value.start.return_value=0
            getattr(device,getter).return_value.stop.return_value=10e9
        order=[]
        device.get_rx_stream.side_effect=lambda args:order.append('stream') or MagicMock()
        device.set_time_now.side_effect=lambda time:order.append('synchronize')
        with patch.dict('sys.modules',{'uhd':fake}):
            receiver=Receiver('192.168.1.10',dict(DEFAULT,channels=[0,1]))
            receiver.close()
        self.assertEqual(order,['stream','synchronize'])

    def test_fft_frequency_and_level(self):
        import numpy as np
        n=1024;rate=1e6
        samples=0.5*np.exp(2j*np.pi*64*np.arange(n)/n)
        frame=spectrum(samples,rate,100e6,n,2)
        peak=int(np.argmax(frame['power_dbfs']))
        self.assertEqual(peak,512+64)
        self.assertAlmostEqual(frame['power_dbfs'][peak],20*np.log10(0.5)+2,places=3)
        with self.assertRaises(ValueError): spectrum(samples,rate,100e6,1000)

    def test_trigger_retains_exact_bounded_pretrigger_tail(self):
        import numpy as np
        trigger=Trigger(100,0.05,-6)
        self.assertEqual(trigger.accept(np.zeros((1,4),dtype=np.complex64)),[])
        self.assertEqual(trigger.accept(np.zeros((1,4),dtype=np.complex64)),[])
        self.assertEqual(trigger.count,5)
        result=trigger.accept(np.ones((1,3),dtype=np.complex64))
        self.assertTrue(trigger.fired)
        self.assertEqual(sum(x.shape[1] for x in result),8)
        self.assertEqual(len(trigger.blocks),0)
        self.assertEqual(len(trigger.accept(np.ones((1,2)))),1)


def release(root):
    def image(payload):
        header=bytearray(64)
        struct.pack_into('!7I',header,0,0x27051956,0,0,len(payload),0,0,zlib.crc32(payload))
        struct.pack_into('!I',header,4,zlib.crc32(header))
        return bytes(header)+payload
    for filename in ops.FILES:
        (root/filename).write_bytes(image(b'image') if filename in ('uImage','uramdisk.image.gz','boot.scr') else b'boot file')
    info={'board':'e200','source_revision':'a'*40,'boot_protocol':1,'source_dirty':False}
    (root/'BUILD_INFO.json').write_text(json.dumps(info))
    sums=''.join(hashlib.sha256((root/name).read_bytes()).hexdigest()+'  '+name+'\n' for name in ops.FILES)
    (root/'SHA256SUMS').write_text(sums)
    return info


class ReleaseTest(unittest.TestCase):
    def test_legacy_fallback_health_remains_observable(self):
        class Connection:
            host='192.168.1.10'
            def run(self,command,**kwargs):
                return {'/etc/init.d/S90device_start.sh status':'OK\n',
                        'cat /etc/antsdr-release.json':'{"board":"e200"}',
                        'cat /sys/class/net/eth0/address':'02:11:22:33:44:55\n',
                        'cat /sys/class/net/eth0/carrier':'1\n',
                        'cat /proc/uptime':'10.5 0\n',
                        'cat /proc/cmdline':'antsdr.slot=root panic=10\n'}.get(command,'')
        with patch('antsdr.ops.discover',return_value={'model':'E200'}):
            status=ops.health(Connection())
        self.assertTrue(status['healthy']);self.assertTrue(status['legacy_health'])
        self.assertEqual(status['boot_slot'],'root')

    def test_release_integrity_and_path_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);info=release(root)
            self.assertEqual(ops.verify_release(root),info)
            (root/'uImage').write_bytes(b'damaged')
            with self.assertRaises(ValueError):ops.verify_release(root)
            release(root)
            sums=root/'SHA256SUMS'
            sums.write_text(sums.read_text()+('0'*64)+'  ../outside\n')
            with self.assertRaises(ValueError):ops.verify_release(root)

    def test_transport_rejects_malformed_or_wrong_board_before_activation(self):
        class Connection:
            def __init__(self):self.commands=[]
            def run(self,command,**kwargs):
                self.commands.append(command)
                return '{"board":"e310v2"}'
        with tempfile.TemporaryDirectory() as folder:
            release(Path(folder));connection=Connection()
            with self.assertRaises(ValueError):ops.deploy(connection,folder)
            self.assertEqual(connection.commands,['cat /etc/antsdr-release.json'])

    def test_checksum_failure_never_publishes_trial(self):
        class Connection:
            def __init__(self):self.commands=[];self.uploads=[]
            def run(self,command,**kwargs):
                self.commands.append(command)
                if command=='cat /etc/antsdr-release.json':return '{"board":"e200"}'
                if 'cat '+ops.BOOT+'/antsdr-active' in command:return 'b'
                if 'sha256sum -c' in command:raise RuntimeError('Injected device integrity failure')
                return ''
            def upload(self,local,remote):self.uploads.append(remote)
        with tempfile.TemporaryDirectory() as folder:
            release(Path(folder));connection=Connection()
            with self.assertRaises(RuntimeError):ops.deploy(connection,folder)
            self.assertEqual(len(connection.uploads),9)
            self.assertFalse(any('antsdr-trial.new' in command for command in connection.commands))

    def test_rollback_of_pending_trial_retains_confirmed_image(self):
        class Connection:
            def __init__(self):self.commands=[]
            def run(self,command,**kwargs):
                self.commands.append(command)
                if '/antsdr-trial ' in command:return 'b'
                if '/antsdr-active ' in command:return 'a'
                return ''
        connection=Connection()
        result=ops.rollback(connection)
        self.assertEqual(result,{'selected_slot':'a','cancelled_trial':'b'})
        self.assertFalse(any('antsdr-active.new' in command for command in connection.commands))


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.app=Application(Store(self.folder.name),'192.168.1.10',None)
        self.server=make_server(self.app,0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        self.port=self.server.server_port

    def request(self,method,path,body=None,headers=None):
        client=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        client.request(method,path,body,headers or {})
        response=client.getresponse();status=response.status;data=response.read();client.close()
        return status,data

    def test_static_dashboard_and_health_api(self):
        status,body=self.request('GET','/')
        self.assertEqual(status,200);self.assertIn(b'Waterfall',body)
        status,body=self.request('GET','/api/state')
        self.assertEqual(status,200);self.assertIn('health',json.loads(body))
        status,_=self.request('GET','/../../etc/passwd');self.assertEqual(status,404)

    def test_mutations_require_token_and_same_origin(self):
        body=json.dumps(DEFAULT)
        status,_=self.request('POST','/api/profiles',body,{'Content-Type':'application/json'})
        self.assertEqual(status,403)
        headers={'Content-Type':'application/json','X-AntSDR-Token':self.app.token,'Origin':'http://evil.invalid'}
        status,_=self.request('POST','/api/profiles',body,headers);self.assertEqual(status,403)
        headers.pop('Origin')
        status,_=self.request('POST','/api/profiles',body,headers);self.assertEqual(status,200)
        self.assertEqual(self.app.store.get('profiles','e200-rx'),DEFAULT)

    def test_dns_rebinding_host_is_rejected(self):
        status,_=self.request('GET','/api/session',headers={'Host':'evil.invalid'})
        self.assertEqual(status,403)


if __name__=='__main__':unittest.main()
