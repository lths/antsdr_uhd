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
    sub.add_parser('health')
    sub.add_parser('diagnostics')
    sub.add_parser('sync-settings')
    sub.add_parser('pull-settings')
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
    elif args.command in ('capture','benchmark'):
        from .radio import Receiver
        settings=store.get('profiles',args.profile)
        calibration=store.get('calibrations',settings['calibration']) if settings.get('calibration') else None
        results=[]
        for rate in args.rates if args.command=='benchmark' else [settings['sample_rate']]:
            setting=dict(settings,sample_rate=number(rate,1,61.44e6,'sample_rate'))
            receiver=Receiver(args.address,setting,calibration)
            try:
                kwargs=({'output':args.output,'threshold_dbfs':args.threshold_dbfs,
                         'pretrigger_seconds':args.pretrigger_seconds,'trigger_timeout':args.trigger_timeout,
                         'trigger_after_seconds':args.trigger_after}
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
