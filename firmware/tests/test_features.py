import json
import os
from pathlib import Path
import subprocess
import time

from test_firmware import FIRMWARE, SandboxTest
from test_runtime import RuntimeTest


class SettingsTest(RuntimeTest):
    def setUp(self):
        super().setUp()
        self.redirect(FIRMWARE/'buildroot/board/antsdr/persistence.sh','persistence.sh')
        self.mounts=self.write('mounts','')
        (self.root/'run').mkdir()

    def mounted(self):
        self.mounts.write_text(f'/dev/mtdblock2 {self.root}/persistent jffs2 rw 0 0\n')

    def test_sd_store_is_private_and_restores_only_checksummed_files(self):
        state=self.root/'sd-state';state.mkdir()
        self.mounts.write_text(f'/dev/loop0 {state} ext4 rw,nodev 0 0\n')
        config=self.redirect(FIRMWARE/'buildroot/board/antsdr/antsdr-config','settings')
        self.write('etc/antsdr/profile.receive.json','{"schema_version":1}\n')
        self.run_command(['sh',str(config),'save'])
        store=state/'.antsdr';generation=store/(store/'settings.current').read_text().strip()
        self.assertEqual((generation/'profile.receive.json').stat().st_mode&0o777,0o600)
        self.write('etc/antsdr/profile.receive.json','changed')
        (generation/'unlisted.conf').write_text('unverified')
        self.run_command(['sh',str(config),'restore'])
        self.assertEqual((self.root/'etc/antsdr/profile.receive.json').read_text(),'{"schema_version":1}\n')
        self.assertFalse((self.root/'etc/antsdr/unlisted.conf').exists())
        (generation/'profile.receive.json').write_text('corrupt')
        self.run_command(['sh',str(config),'restore'],success=False)

    def test_settings_reject_symlinks_and_path_traversal(self):
        self.mounted()
        config=self.redirect(FIRMWARE/'buildroot/board/antsdr/antsdr-config','settings')
        self.write('etc/antsdr/valid.json','{}\n')
        (self.root/'etc/antsdr/escape.json').symlink_to('/etc/passwd')
        self.run_command(['sh',str(config),'save'],success=False)
        (self.root/'etc/antsdr/escape.json').unlink()
        self.run_command(['sh',str(config),'save'])
        store=self.root/'persistent/.antsdr';generation=store/(store/'settings.current').read_text().strip()
        (generation/'settings.sha256').write_text('0'*64+'  ../../etc/shadow\n')
        self.run_command(['sh',str(config),'restore'],success=False)


class IdentityTest(RuntimeTest):
    def setUp(self):
        super().setUp()
        self.write('mounts',f'/dev/loop0 {self.root}/sd-state ext4 rw 0 0\n')
        (self.root/'sd-state').mkdir()
        self.redirect(FIRMWARE/'buildroot/board/antsdr/persistence.sh','persistence.sh')
        self.write('cid','6f0303534441424310aa002494018a00\n')
        self.write('mac','12:34:56:78:9a:bc\n')
        self.write('assignment','1\n')
        self.write('bin/sync','#!/bin/sh\nexit 0\n',executable=True)
        self.write('bin/ip',f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{self.root}/ip-calls"\n',executable=True)
        source=FIRMWARE/'buildroot/board/antsdr/S35identity'
        text=source.read_text().replace('/sys/block/mmcblk0/device/cid',str(self.root/'cid')).replace('/sys/class/net/eth0/address',str(self.root/'mac')).replace('/sys/class/net/eth0/addr_assign_type',str(self.root/'assignment'))
        self.script=self.redirect(self.write('identity-source',text),'identity')

    def test_identity_is_stable_and_locally_administered(self):
        self.run_command(['sh',str(self.script),'start'])
        mac=(self.root/'sd-state/mac-address').read_text()
        self.assertTrue(mac.startswith('02:'))
        self.write('cid','0'*32+'\n')
        self.run_command(['sh',str(self.script),'start'])
        self.assertEqual((self.root/'sd-state/mac-address').read_text(),mac)
        self.assertIn('address '+mac.strip(),(self.root/'ip-calls').read_text())

    def test_invalid_persisted_identity_is_rejected(self):
        self.write('sd-state/mac-address','ff:ff:ff:ff:ff:ff\n')
        self.run_command(['sh',str(self.script),'start'],success=False)
        self.assertFalse((self.root/'ip-calls').exists())

    def test_factory_address_is_preserved(self):
        self.write('assignment','0\n')
        self.write('mac','00:0a:35:12:34:56\n')
        self.run_command(['sh',str(self.script),'start'])
        self.assertEqual((self.root/'sd-state/mac-address').read_text(),'00:0a:35:12:34:56\n')
        self.assertFalse((self.root/'ip-calls').exists())


class BootConfirmationTest(RuntimeTest):
    def setUp(self):
        super().setUp()
        self.write('cmdline','console=test antsdr.slot=a panic=10\n')
        self.write('sd-boot/antsdr-trial','a')
        self.write('sd-boot/antsdr-attempted','1')
        self.write('sd-boot/antsdr-active','b')
        self.write('etc/antsdr-release.json','{"revision":"test"}\n')
        self.write('sd-boot/antsdr-slots/a/BUILD_INFO.json','{"revision":"test"}\n')
        self.run_command(['sh','-c','cd sd-boot/antsdr-slots/a && sha256sum BUILD_INFO.json > SHA256SUMS'])
        self.write('bin/antsdr-health','#!/bin/sh\n[ "${FAIL_HEALTH:-no}" = no ]\n',executable=True)
        self.write('bin/sleep','#!/bin/sh\nexit 0\n',executable=True)
        self.write('bin/sync','#!/bin/sh\nexit 0\n',executable=True)
        self.write('bin/logger','#!/bin/sh\nexit 0\n',executable=True)
        self.write('bin/reboot',f'#!/bin/sh\ntouch "{self.root}/rebooted"\n',executable=True)
        (self.root/'run').mkdir()
        self.script=self.redirect(FIRMWARE/'buildroot/board/antsdr/S96bootconfirm','confirm')

    def test_only_healthy_matching_slot_is_confirmed(self):
        self.run_command(['sh',str(self.script),'start'])
        self.assertEqual((self.root/'sd-boot/antsdr-active').read_text(),'a')
        self.assertEqual((self.root/'sd-boot/antsdr-previous').read_text(),'b')
        self.assertFalse((self.root/'sd-boot/antsdr-trial').exists())
        self.assertFalse((self.root/'rebooted').exists())

    def test_failed_health_or_integrity_preserves_previous_slot_and_reboots(self):
        for failure in ('health','integrity'):
            with self.subTest(failure=failure):
                if failure=='health':self.env['FAIL_HEALTH']='yes'
                else:
                    self.env.pop('FAIL_HEALTH',None)
                    self.write('sd-boot/antsdr-slots/a/BUILD_INFO.json','corrupt')
                self.run_command(['sh',str(self.script),'start'],success=False)
                self.assertEqual((self.root/'sd-boot/antsdr-active').read_text(),'b')
                self.assertTrue((self.root/'sd-boot/antsdr-trial').exists())
                self.assertTrue((self.root/'rebooted').exists())

    def test_fallback_boot_never_confirms_the_failed_trial(self):
        self.write('cmdline','antsdr.slot=b panic=10\n')
        self.run_command(['sh',str(self.script),'start'])
        self.assertEqual((self.root/'sd-boot/antsdr-active').read_text(),'b')
        self.assertTrue((self.root/'sd-boot/antsdr-trial').exists())


class HealthTest(SandboxTest):
    def setUp(self):
        super().setUp()
        self.binary=self.root/'health'
        subprocess.run(['cc','-D_DEFAULT_SOURCE','-std=c11','-O2','-Wall','-Wextra','-Werror',
                        '-DHEALTH_ROOT="'+str(self.root)+'"','-DHEALTH_INTERVAL=0.03',
                        '-DHEALTH_SLEEP_NS=10000000','-DHEALTH_PROBE_US=1000',
                        str(FIRMWARE/'buildroot/board/antsdr/health.c'),'-o',str(self.binary)],check=True)
        self.write('sys/class/net/eth0/carrier','1\n')
        self.write('sys/class/net/eth0/address','02:11:22:33:44:55\n')
        self.write('etc/antsdr-release.json','{"board":"e200"}\n')
        self.write('proc/uptime','10.5 0\n');self.write('proc/loadavg','0.01 0 0\n')
        self.write('proc/meminfo','MemAvailable: 1234 kB\n')
        self.write('etc/init.d/S90device_start.sh',f'#!/bin/sh\necho restart >> "{self.root}/recoveries"\n',executable=True)
        (self.root/'run').mkdir()

    def test_json_exposes_missing_processes_and_identity(self):
        result=self.run_command([str(self.binary),'--json'],success=False)
        report=json.loads(result.stdout)
        self.assertFalse(report['healthy']);self.assertFalse(report['radio_running'])
        self.assertEqual(report['firmware']['board'],'e200')
        self.assertEqual(report['memory_available_kb'],1234)

    def test_recovery_is_bounded_and_maintenance_suppresses_restarts(self):
        for maintenance in (False,True):
            (self.root/'run/antsdr-health.json').unlink(missing_ok=True)
            if maintenance:self.write('run/antsdr-maintenance','')
            process=subprocess.Popen([str(self.binary),'--supervise'],stdout=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    report_file=self.root/'run/antsdr-health.json'
                    if report_file.exists():
                        report=json.loads(report_file.read_text())
                        if (maintenance and report['maintenance']) or report['recovery_limited']:break
                    time.sleep(0.02)
                else:self.fail('Supervisor did not reach the expected state')
                if maintenance:self.assertEqual(report['restarts'],0)
                else:self.assertEqual(report['restarts'],3)
            finally:
                process.terminate();process.wait(timeout=2)


class BootSelectionTest(SandboxTest):
    def run_boot(self,active=None,trial=None,attempted=False,bad=None,write_fails=False):
        for item in ('antsdr-active','antsdr-trial','antsdr-attempted','boots'):
            (self.root/item).unlink(missing_ok=True)
        if active:self.write('antsdr-active',active)
        if trial:self.write('antsdr-trial',trial)
        if attempted:self.write('antsdr-attempted','1')
        source=(FIRMWARE/'scripts/boot.cmd').read_text().replace('itest.b','memory_test').replace('mw.b','memory_write').replace('test -e mmc 0 ','exists_mmc ')
        harness=r'''
            setenv() { key=$1; shift; export "$key=$*"; }
            run() { for key in "$@"; do eval "value=\$$key"; eval "$value" || return $?; done; }
            memory_test() { [ "$memory" = "$3" ]; }
            memory_write() { memory=1; }
            exists_mmc() { [ -f "$1" ]; }
            fatload() {
                case "$4" in
                    antsdr-active|antsdr-trial)
                        [ -f "$4" ] || return 1
                        case "$(cat "$4")" in a) memory=0x61 ;; b) memory=0x62 ;; *) memory=invalid ;; esac
                        ;;
                    *) [ "${BAD_SLOT:-none}" != "$antsdr_slot" ] || return 1 ;;
                esac
                filesize=100
            }
            fatwrite() { [ "${WRITE_FAILS:-no}" = no ] || return 1; printf 1 > antsdr-attempted; }
            fpga() { return 0; }
            bootm() { printf '%s\n' "$antsdr_slot" >> boots; exit 0; }
            antsdr_state_addr=0x100; antsdr_script_addr=0x200
            loadbit_addr=0x300; fit_load_address=0x400; devicetree_load_address=0x500; ramdisk_load_address=0x600
            bootargs=console=test
        '''
        script=self.write('boot-test',harness+'\n'+source)
        self.env['BAD_SLOT']=bad or 'none';self.env['WRITE_FAILS']='yes' if write_fails else 'no'
        self.run_command(['sh',str(script)])
        return (self.root/'boots').read_text().strip()

    def test_initial_and_confirmed_boots(self):
        self.assertEqual(self.run_boot(),'root')
        self.assertEqual(self.run_boot(active='b'),'b')

    def test_trial_attempt_is_recorded_before_boot(self):
        self.assertEqual(self.run_boot(active='b',trial='a'),'a')
        self.assertTrue((self.root/'antsdr-attempted').exists())

    def test_unconfirmed_or_unloadable_trial_falls_back(self):
        self.assertEqual(self.run_boot(active='b',trial='a',attempted=True),'b')
        self.assertEqual(self.run_boot(active='b',trial='a',bad='a'),'b')
        self.assertEqual(self.run_boot(active='b',trial='a',write_fails=True),'b')
        self.assertEqual(self.run_boot(active='b',trial='invalid'),'b')
