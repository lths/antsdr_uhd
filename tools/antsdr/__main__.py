import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

from . import ops
from .store import Store, atomic_json, name, number

DEFAULT = {'schema_version':1, 'name':'e200-rx', 'frequency_hz':1e9,
           'sample_rate':1e6, 'bandwidth_hz':1e6, 'gain_db':10, 'channels':[0],
           'antenna':'RX2', 'clock_source':'internal'}


def parser():
    p=argparse.ArgumentParser(description='E200 deployment, health and receive tools')
    p.add_argument('--address',default='192.168.1.10',type=ops.address)
    p.add_argument('--state',type=Path,default=Path('.antsdr'))
    p.add_argument('--known-hosts',type=Path)
    p.add_argument('--identity',type=str)
    p.add_argument('--password-env',help='Name of an environment variable containing the SSH/console password')
    p.add_argument('--serial',help='Serial port used to authenticate the public SSH host key')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('trust-console')
    sub.add_parser('discover')
    inspect=sub.add_parser('radio-info');inspect.add_argument('--profile',default='e200-rx')
    sub.add_parser('health')
    sub.add_parser('diagnostics')
    sub.add_parser('sync-settings')
    sub.add_parser('pull-settings')
    sub.add_parser('gps-status')
    timing = sub.add_parser('timing')
    timing.add_argument('--profile', default='e200-rx')
    timing.add_argument('--duration', type=float, default=10)
    timing.add_argument('--report', type=Path)
    reference = sub.add_parser('reference-check')
    reference.add_argument('--timeout', type=float, default=60)
    reference.add_argument('--report', type=Path, required=True)
    sweep = sub.add_parser('throughput')
    sweep.add_argument('--profile', default='e200-rx')
    sweep.add_argument('--rates', nargs='+', type=float, default=[1e6, 5e6, 10e6, 15e6, 20e6, 25e6])
    sweep.add_argument('--duration', type=float, default=5)
    sweep.add_argument('--all-channels', action='store_true')
    sweep.add_argument('--report', type=Path, required=True)
    soak = sub.add_parser('soak')
    soak.add_argument('--profile', default='e200-rx')
    soak.add_argument('--duration', type=float, default=3600)
    soak.add_argument('--report', type=Path, required=True)
    soak.add_argument('--output', type=Path)
    analyze = sub.add_parser('analyze')
    analyze.add_argument('--input', type=Path, required=True)
    analyze.add_argument('--tone-hz', type=float)
    analyze.add_argument('--reference-dbm', type=float)
    analyze.add_argument('--reference')
    analyze.add_argument('--step-at', type=float, help='Known input step offset in seconds, for AGC/overload recovery analysis')
    analyze.add_argument('--report', type=Path)
    sigmf = sub.add_parser('sigmf')
    sigmf.add_argument('--input', type=Path, required=True)
    sigmf.add_argument('--prefix', type=Path, required=True)
    response = sub.add_parser('filter-response')
    response.add_argument('--passband', type=Path, required=True)
    response.add_argument('--stopband', type=Path, required=True)
    response.add_argument('--reference', required=True)
    response.add_argument('--report', type=Path)
    scan = sub.add_parser('scan')
    scan.add_argument('--profile', default='e200-rx')
    scan.add_argument('--frequencies', nargs='+', type=float, required=True, help='RX frequencies in Hz, up to eight')
    scan.add_argument('--dwell', type=float, default=.025)
    scan.add_argument('--cycles', type=int, default=1)
    scan.add_argument('--report', type=Path, required=True)
    for kind in ('profile','calibration'):
        record=sub.add_parser(kind).add_subparsers(dest='action',required=True)
        record.add_parser('list')
        record.add_parser('put').add_argument('file',type=Path)
    verify=sub.add_parser('verify');verify.add_argument('directory',type=Path)
    deploy=sub.add_parser('deploy');deploy.add_argument('directory',type=Path);deploy.add_argument('--reboot',action='store_true')
    rollback=sub.add_parser('rollback');rollback.add_argument('--reboot',action='store_true')
    serve=sub.add_parser('serve');serve.add_argument('--port',type=int,default=8088)
    for command in ('capture','benchmark'):
        c=sub.add_parser(command)
        c.add_argument('--profile',default='e200-rx')
        c.add_argument('--duration',type=float,default=3)
        c.add_argument('--report',type=Path)
        if command=='capture':
            c.add_argument('--output',type=Path,required=True)
            c.add_argument('--threshold-dbfs',type=float)
            c.add_argument('--pretrigger-seconds',type=float,default=0)
            c.add_argument('--trigger-timeout',type=float,default=10)
            c.add_argument('--trigger-after',type=float,help='Trigger after this many seconds of receive time')
            c.add_argument('--trigger-mode', choices=('host','device-time','fpga-level','external-edge'), default='host')
            c.add_argument('--device-delay',type=float,help='Device-timed start, seconds after the current device time (at least 0.1)')
        else:
            c.add_argument('--rates',nargs='+',type=float,default=[1e6,5e6,10e6])
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    store=Store(args.state)
    if not (store.root/'profiles/e200-rx.json').exists():
        store.put('profiles',DEFAULT)
    known_hosts=args.known_hosts or args.state/'known_hosts'
    password=None
    if args.password_env:
        if args.password_env not in os.environ:
            raise ValueError('Password environment variable is unset')
        password=os.environ[args.password_env]
    def connect():
        return ops.SSH(args.address,known_hosts,password,args.identity)
    def trust():
        if not args.serial:
            raise ValueError('Use --serial to verify the host key through the E200 console')
        return ops.trust_console(args.address,args.serial,known_hosts,password)
    def reboot_and_check(expected=None, expected_slot=None):
        # The asynchronous reboot permits SSH to finish sending its reply.
        with connect() as connection:
            connection.run('sync; (sleep 1; reboot) >/dev/null 2>&1 </dev/null &')
        time.sleep(3)
        deadline=time.monotonic()+90
        last='Device did not return'
        while time.monotonic()<deadline:
            try:
                result=ops.discover(args.address)
                break
            except (OSError,ValueError) as error:
                last=str(error);time.sleep(1)
        else: raise TimeoutError(last)
        # The first migration may establish a new key; accept only a console-verified key.
        if args.serial:
            trust()
        with connect() as connection:
            confirmation_deadline=time.monotonic()+40
            while True:
                status=ops.health(connection)
                if not expected_slot or (status['confirmed_slot']==expected_slot and status['trial_slot'] is None):
                    break
                if status['boot_slot']!=expected_slot or time.monotonic()>=confirmation_deadline:
                    raise RuntimeError('Requested trial slot was not confirmed')
                time.sleep(0.25)
            if not status['healthy']:
                raise RuntimeError('Firmware returned but its service health check failed')
            if expected and status['firmware']['source_revision']!=expected:
                raise RuntimeError('Trial did not activate; the running firmware differs from the requested release')
            if status['firmware'].get('boot_protocol') == 1:
                connection.run('device_persistent_keys')
            result['health']=status
        return result

    result=None
    if args.command=='trust-console': result=trust()
    elif args.command=='discover': result=ops.discover(args.address)
    elif args.command=='verify': result=ops.verify_release(args.directory)
    elif args.command in ('profile','calibration'):
        kind={'profile':'profiles','calibration':'calibrations'}[args.command]
        result=store.put(kind,json.loads(args.file.read_text())) if args.action=='put' else store.list(kind)
    elif args.command=='serve':
        from .dashboard import Application,serve
        if not 1024<=args.port<=65535: raise ValueError('Choose an unprivileged TCP port')
        serve(Application(store,args.address,connect),args.port)
        return 0
    elif args.command=='gps-status':
        from .gpsdo import snapshot
        result = snapshot(store.root/'timing/gpsdo.json')
    elif args.command=='reference-check':
        from .reference import check
        if args.report.exists(): raise ValueError('Report destination already exists')
        result = check(args.address, args.report, args.timeout)
        print(json.dumps(result,indent=2,allow_nan=False))
        return 0 if result['complete'] else 1
    elif args.command=='scan':
        from .scan import run
        setting = store.get('profiles', args.profile)
        calibration = store.get('calibrations', setting['calibration']) if setting.get('calibration') else None
        if args.report.exists(): raise ValueError('Report destination already exists')
        result = run(args.address, setting, args.frequencies, args.dwell, args.cycles, args.report, calibration=calibration)
        print(json.dumps(result,indent=2,allow_nan=False))
        return 0 if result['complete'] else 1
    elif args.command=='sigmf':
        from .recordings import export_sigmf
        result = export_sigmf(args.input, args.prefix)
    elif args.command=='analyze':
        from .measurements import analyze_capture
        result = analyze_capture(args.input, args.tone_hz, args.reference_dbm, args.reference, args.step_at)
        if args.report: atomic_json(args.report, result)
    elif args.command=='filter-response':
        from .measurements import filter_response
        result = filter_response(args.passband, args.stopband, args.reference)
        if args.report: atomic_json(args.report, result)
    elif args.command=='timing':
        from .radio import Receiver
        number(args.duration, 1, 3600, 'duration')
        receiver = Receiver(args.address, store.get('profiles', args.profile))
        try:
            time.sleep(args.duration)
            result = receiver.timing.status()
        finally: receiver.close()
        if args.report: atomic_json(args.report, result)
    elif args.command in ('throughput','soak'):
        from .measurements import throughput, endurance
        setting = store.get('profiles', args.profile)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        if args.report.exists(): raise ValueError('Report destination already exists')
        result = (throughput(args.address, setting, args.rates, args.duration,
                             [[0], [1], [0,1]] if args.all_channels else None, args.report)
                  if args.command=='throughput' else
                  endurance(args.address, setting, args.duration, args.report, output=args.output))
        print(json.dumps(result,indent=2,allow_nan=False))
        return 0 if result['complete'] else 1
    elif args.command=='radio-info':
        from .radio import Receiver
        setting=store.get('profiles',args.profile)
        correction=store.get('calibrations',setting['calibration']) if setting.get('calibration') else None
        receiver=Receiver(args.address,setting,correction)
        try: result=receiver.describe()
        finally: receiver.close()
    elif args.command in ('capture','benchmark'):
        from .radio import Receiver
        settings=store.get('profiles',args.profile)
        calibration=store.get('calibrations',settings['calibration']) if settings.get('calibration') else None
        results=[]
        for rate in args.rates if args.command=='benchmark' else [settings['sample_rate']]:
            setting=dict(settings,sample_rate=number(rate,1,61.44e6,'sample_rate'))
            trigger_mode=args.trigger_mode if args.command=='capture' else 'host'
            if args.command=='capture' and trigger_mode not in ('host','fpga-level') and args.threshold_dbfs is not None:
                raise ValueError('Threshold requires a level trigger')
            receiver=Receiver(args.address,setting,calibration,trigger_mode=trigger_mode,
                hardware_threshold_dbfs=args.threshold_dbfs if trigger_mode=='fpga-level' else None)
            try:
                kwargs=({'output':args.output,'threshold_dbfs':args.threshold_dbfs if trigger_mode=='host' else None,
                         'pretrigger_seconds':args.pretrigger_seconds,'trigger_timeout':args.trigger_timeout,
                         'trigger_after_seconds':args.trigger_after,'device_delay_seconds':args.device_delay}
                        if args.command=='capture' else {})
                report=receiver.run(args.duration,**kwargs)
                report['passed']=report['complete'] and not any(report[key] for key in ('overflows','timeouts','other_errors')) and report['received_samples_per_channel']>=receiver.rate*args.duration*0.9
                results.append(report)
            finally: receiver.close()
        result=results if args.command=='benchmark' else results[0]
        if args.report: atomic_json(args.report,result)
        print(json.dumps(result,indent=2,allow_nan=False))
        return 0 if all(r['passed'] for r in results) else 1
    else:
        with connect() as connection:
            if args.command=='health': result=ops.health(connection)
            elif args.command=='diagnostics':
                print(connection.run('antsdr-diagnostics'));return 0
            elif args.command=='deploy': result=ops.deploy(connection,args.directory)
            elif args.command=='rollback': result=ops.rollback(connection)
            elif args.command=='sync-settings':
                connection.run('install -d -m 0700 /etc/antsdr')
                for kind,prefix in (('profiles','profile'),('calibrations','calibration')):
                    for value in store.list(kind):
                        local=store.root/kind/(name(value['name'])+'.json')
                        connection.upload(local,'/etc/antsdr/'+prefix+'.'+name(value['name'])+'.json')
                connection.run('antsdr-config save')
                result={'saved':True}
            elif args.command=='pull-settings':
                listing=connection.run('find /etc/antsdr -maxdepth 1 -type f -name "*.json"')
                saved=[]
                for filename in listing.splitlines():
                    match=re.fullmatch(r'/etc/antsdr/(profile|calibration)\.([A-Za-z0-9_.-]+)\.json',filename)
                    if match:
                        kind={'profile':'profiles','calibration':'calibrations'}[match[1]]
                        value=json.loads(connection.run('cat '+shlex.quote(filename)))
                        if value['name']!=match[2]: raise ValueError('Stored record name mismatch')
                        store.put(kind,value);saved.append(value['name'])
                result={'restored':saved}
        if args.command in ('deploy','rollback') and args.reboot:
            result['verification']=reboot_and_check(result['release']['source_revision'] if args.command=='deploy' else None,
                                                    result['slot'] if args.command=='deploy' else None)
    print(json.dumps(result,indent=2,allow_nan=False))
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (Exception,KeyboardInterrupt) as error:
        print('antsdr: '+str(error),file=sys.stderr)
        raise SystemExit(1)
