"""Exercise boot and credential scripts with device paths redirected to a sandbox."""

import os
import json
import re
from pathlib import Path
import shutil
import stat

from test_firmware import BOARDS, FIRMWARE, SandboxTest


class RuntimeTest(SandboxTest):
    def redirect(self, source, dest):
        text = source.read_text()
        # Replace longer paths first; no actual device operations are permitted.
        replacements = {
            '/usr/lib/antsdr/persistence.sh': str(self.root / 'persistence.sh'),
            '/proc/mounts': str(self.root / 'mounts'),
            '/proc/net/dev': str(self.root / 'net-dev'),
            '/mnt/jffs2': str(self.root / 'persistent'),
            '/etc': str(self.root / 'etc'),
            '/root/.ssh': str(self.root / 'ssh'),
            '/run': str(self.root / 'run'),
            '/sbin/ifup': str(self.bin / 'ifup'),
            '/sbin/ifdown': str(self.bin / 'ifdown'),
            '/bin/hostname': str(self.bin / 'hostname'),
            '/sbin/net_set': str(self.bin / 'net_set'),
            '/sbin/e200': str(self.bin / 'e200'),
            '/sbin/e310v2': str(self.bin / 'e310v2'),
            '/bin/ping': str(self.bin / 'ping'),
            '/sbin/$DEVICE': str(self.bin / '$DEVICE'),
        }
        pattern = r'(?<![A-Za-z0-9_])(?:' + '|'.join(re.escape(key) for key in replacements) + ')'
        text = re.sub(pattern, lambda match: replacements[match.group()], text)
        return self.write(dest, text, executable=True)

    def script(self, board, name):
        return self.redirect(FIRMWARE / f'buildroot/board/{board}/{name}', f'{board}-{name}')


class NetworkTest(RuntimeTest):
    def setUp(self):
        super().setUp()
        self.interfaces = self.write('etc/network/interfaces', '''
            auto lo eth0 wlan0
            iface lo inet loopback
            iface wlan0 inet dhcp
                wpa-conf /etc/wifi.conf
            iface eth0 inet static
                address 10.0.0.2
                netmask 255.255.255.0
            iface eth0 inet static
                address 10.0.0.3
            iface eth0 inet6 auto
        ''')
        self.write('etc/device_config', f'IFAC="{self.interfaces}"\n')
        self.write('etc/hostname', 'ant\n')
        self.write('bin/fw_printenv', '''
            #!/bin/sh
            case "$2" in
                hostname) echo ant ;;
                ipaddr_eth) printf '%s\\n' "${TEST_IP-192.168.1.10}" ;;
                netmask_eth) echo 255.255.0.0 ;;
            esac
        ''', executable=True)
        for name in ('ifup', 'ifdown', 'hostname'):
            self.write(f'bin/{name}', f'#!/bin/sh\n[ "$FAIL_COMMAND" != "{name}" ]\n', executable=True)
        self.write('bin/logger', '#!/bin/sh\ncat >/dev/null\n', executable=True)

    def test_restarts_preserve_other_interfaces_without_duplicates(self):
        for board in BOARDS:
            with self.subTest(board=board):
                script = self.script(board, 'S40network')
                self.run_command(['sh', str(script), 'start'])
                expected = self.interfaces.read_bytes()
                self.run_command(['sh', str(script), 'restart'])
                self.assertEqual(self.interfaces.read_bytes(), expected)
                text = expected.decode()
                self.assertEqual(text.count('iface eth0 inet static'), 1)
                self.assertEqual(text.count('auto eth0'), 1)
                self.assertIn('auto lo wlan0', text)
                self.assertIn('iface lo inet loopback', text)
                self.assertIn('iface eth0 inet6 auto', text)
                self.assertIn('wpa-conf /etc/wifi.conf', text)
                self.assertNotIn('address 10.0.0.', text)
                self.env['TEST_IP'] = ''
                self.run_command(['sh', str(script), 'start'])
                self.assertIn('iface eth0 inet dhcp', self.interfaces.read_text())
                self.env.pop('TEST_IP')

    def test_command_failures_are_returned(self):
        for board in BOARDS:
            script = self.script(board, 'S40network')
            for failure, action in [('ifup', 'start'), ('ifdown', 'stop'), ('hostname', 'start'), ('ifdown', 'restart')]:
                with self.subTest(board=board, failure=failure, action=action):
                    self.env['FAIL_COMMAND'] = failure
                    self.run_command(['sh', str(script), action], success=False)


class ServiceTest(RuntimeTest):
    def setUp(self):
        super().setUp()
        self.write('bin/start-stop-daemon', f'''
            #!/usr/bin/env python3
            from pathlib import Path
            import os
            import sys
            args = sys.argv[1:]
            exe = args[args.index('--exec') + 1]
            name = Path(exe).name
            pidfile = Path(args[args.index('--pidfile') + 1])
            marker = Path({str(self.root)!r}) / ('running-' + name)
            if '--start' in args:
                if os.environ.get('FAIL_COMMAND') == name:
                    sys.exit(1)
                if os.environ.get('CRASH_COMMAND') == name:
                    sys.exit(0)
                if marker.exists():
                    sys.exit(0)
                pidfile.write_text('12345')
                marker.write_text(' '.join(args))
            elif '--test' in args:
                sys.exit(0 if marker.exists() else 1)
            else:
                if os.environ.get('FAIL_STOP') == name:
                    sys.exit(1)
                marker.unlink(missing_ok=True)
        ''', executable=True)
        for name in ('e200', 'e310v2', 'net_set', 'ping'):
            self.write(f'bin/{name}', '#!/bin/sh\nexit 0\n', executable=True)
        self.write('bin/ip', '#!/bin/sh\necho "2: eth0 inet 192.168.1.10/16 brd 192.168.255.255 scope global eth0"\n', executable=True)
        self.write('bin/sleep', '#!/bin/sh\nexit 0\n', executable=True)

    def test_start_stop_restart_status_and_ping(self):
        for board in BOARDS:
            with self.subTest(board=board):
                script = self.script(board, 'S90device_start.sh')
                self.run_command(['sh', str(script), 'status'], success=False)
                self.run_command(['sh', str(script), 'start'])
                self.assertIn('192.168.255.255', (self.root / 'running-ping').read_text())
                self.run_command(['sh', str(script), 'start'])
                self.run_command(['sh', str(script), 'status'])
                self.run_command(['sh', str(script), 'restart'])
                self.run_command(['sh', str(script), 'stop'])
                self.run_command(['sh', str(script), 'stop'])
                self.run_command(['sh', str(script), 'status'], success=False)
                self.assertEqual(list(self.root.glob('running-*')), [])
                self.assertEqual(list((self.root / 'run').glob('*.pid')), [])

    def test_start_failure_and_immediate_crash_roll_back(self):
        for board in BOARDS:
            script = self.script(board, 'S90device_start.sh')
            for variable in ('FAIL_COMMAND', 'CRASH_COMMAND'):
                with self.subTest(board=board, variable=variable):
                    self.env[variable] = 'net_set'
                    self.run_command(['sh', str(script), 'start'], success=False)
                    self.assertFalse((self.root / f'running-{board}').exists())
                    self.env.pop(variable)

    def test_stop_failure_is_returned(self):
        for board in BOARDS:
            script = self.script(board, 'S90device_start.sh')
            self.run_command(['sh', str(script), 'start'])
            self.env['FAIL_STOP'] = 'net_set'
            self.run_command(['sh', str(script), 'stop'], success=False)
            self.env.pop('FAIL_STOP')
            self.run_command(['sh', str(script), 'stop'])


class PersistenceTest(RuntimeTest):
    def setUp(self):
        super().setUp()
        self.redirect(FIRMWARE / 'buildroot/board/antsdr/persistence.sh', 'persistence.sh')
        (self.root / 'run').mkdir()
        self.mounts = self.write('mounts', '')
        self.write('etc/passwd', 'root:x:0:0:root:/root:/bin/sh\n')
        self.write('etc/group', 'root:x:0:\n')
        self.write('etc/shadow', 'root:secret:0:0:99999:7:::\n')
        self.write('etc/dropbear/dropbear_ecdsa_host_key', 'private-key')
        self.write('ssh/authorized_keys', 'public-key')
        self.write('bin/passwd', f'#!/bin/sh\ntouch "{self.root}/passwd-called"\nexit "${{FAIL_PASSWD:-0}}"\n', executable=True)
        self.write('bin/sync', '#!/bin/sh\nexit "${FAIL_SYNC:-0}"\n', executable=True)

    def snapshot(self, kind):
        store = self.root / 'persistent/.antsdr'
        return store / (store / f'{kind}.current').read_text().strip()

    def mounted(self):
        self.mounts.write_text(f'/dev/mtdblock2 {self.root}/persistent jffs2 rw,noatime 0 0\n')

    def test_unmounted_or_readonly_storage_stops_before_changes(self):
        for board in BOARDS:
            for mount in ('', f'/dev/mtdblock2 {self.root}/persistent jffs2 ro 0 0\n'):
                self.mounts.write_text(mount)
                for name in ('device_passwd', 'device_persistent_keys'):
                    with self.subTest(board=board, command=name, mount=mount):
                        self.run_command(['sh', str(self.script(board, name))], success=False)
                        self.assertFalse((self.root / 'passwd-called').exists())
                        self.assertFalse((self.root / 'persistent').exists())

    def test_save_restore_and_permissions_for_both_boards(self):
        self.mounted()
        for board in BOARDS:
            with self.subTest(board=board):
                for name in ('device_passwd', 'device_persistent_keys'):
                    self.run_command(['sh', str(self.script(board, name))])
                store = self.root / 'persistent/.antsdr'
                self.assertTrue((store / 'passwords.current').is_file())
                self.assertEqual(stat.S_IMODE((self.snapshot('passwords') / 'shadow').stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((self.snapshot('dropbear') / 'dropbear_ecdsa_host_key').stat().st_mode), 0o600)
                self.write('etc/shadow', 'changed')
                self.write('etc/dropbear/dropbear_ecdsa_host_key', 'changed')
                self.write('ssh/authorized_keys', 'changed')
                restore = self.script(board, 'S21misc')
                self.run_command(['sh', str(restore), 'start'])
                self.run_command(['sh', str(restore), 'start'])
                self.assertIn('secret', (self.root / 'etc/shadow').read_text())
                self.assertEqual((self.root / 'etc/dropbear/dropbear_ecdsa_host_key').read_text(), 'private-key')
                self.assertEqual((self.root / 'ssh/authorized_keys').read_text(), 'public-key')
                self.assertEqual(stat.S_IMODE((self.root / 'ssh').stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((self.root / 'etc/shadow').stat().st_mode), 0o600)
                # Publishing another generation retires the previous snapshot.
                self.run_command(['sh', str(self.script(board, 'device_passwd'))])
                self.assertEqual(len(list(store.glob('passwords.??????'))), 1)

    def test_failed_save_preserves_previous_generation(self):
        self.mounted()
        for board in BOARDS:
            save = self.script(board, 'device_passwd')
            self.run_command(['sh', str(save)])
            current = self.root / 'persistent/.antsdr/passwords.current'
            before = current.read_text()
            self.env['FAIL_SYNC'] = '1'
            self.run_command(['sh', str(save)], success=False)
            self.env.pop('FAIL_SYNC')
            self.assertEqual(current.read_text(), before)
            self.assertEqual(len(list(current.parent.glob('passwords.??????'))), 1)
            self.env['FAIL_PASSWD'] = '1'
            self.run_command(['sh', str(save)], success=False)
            self.env.pop('FAIL_PASSWD')
            self.assertEqual(current.read_text(), before)

    def test_corrupt_snapshot_is_not_restored(self):
        self.mounted()
        for board in BOARDS:
            self.run_command(['sh', str(self.script(board, 'device_passwd'))])
            (self.snapshot('passwords') / 'shadow').write_text('corrupt')
            self.write('etc/shadow', 'current')
            self.run_command(['sh', str(self.script(board, 'S21misc')), 'start'], success=False)
            self.assertEqual((self.root / 'etc/shadow').read_text(), 'current')

    def test_legacy_credentials_are_still_restored(self):
        self.mounted()
        for board in BOARDS:
            legacy = self.root / 'persistent/etc'
            legacy.mkdir(parents=True, exist_ok=True)
            for name in ('passwd', 'shadow', 'group'):
                shutil.copyfile(self.root / 'etc' / name, legacy / name)
            self.run_command(['sh', '-c', f'cd "{legacy}" && md5sum passwd shadow group > password.md5'])
            self.write('etc/shadow', 'changed')
            self.run_command(['sh', str(self.script(board, 'S21misc')), 'start'])
            self.assertIn('secret', (self.root / 'etc/shadow').read_text())


class DiagnosticsTest(RuntimeTest):
    def test_network_report_uses_busybox_supported_commands(self):
        self.write('mounts', '')
        self.write('net-dev', 'eth0: 1234 bytes 12 packets\n')
        self.write('bin/ip', '''
            #!/bin/sh
            case "$1" in
                link|-4|route) echo 'network command supported' ;;
                *) echo 'unsupported ip option'; exit 1 ;;
            esac
        ''', executable=True)
        script = self.redirect(FIRMWARE / 'buildroot/board/antsdr/antsdr-diagnostics', 'diagnostics')
        output = self.run_command(['sh', str(script)]).stdout
        self.assertIn('eth0: 1234 bytes 12 packets', output)
        self.assertNotIn('unsupported ip option', output)

    def test_missing_optional_tools_do_not_abort_report(self):
        source = FIRMWARE / 'buildroot/board/antsdr/antsdr-diagnostics'
        self.write('mounts', '')
        script = self.redirect(source, 'diagnostics')
        for name in ('ip', 'ethtool', 'dmesg'):
            self.write(f'bin/{name}', '#!/bin/sh\nexit 1\n', executable=True)
        output = self.run_command(['sh', str(script)]).stdout
        for section in ('System and firmware', 'Device service', 'Network', 'Memory and load', 'Persistent storage', 'Recent kernel messages'):
            self.assertIn(section, output)
        self.assertIn('JFFS2 not mounted', output)
        self.assertIn('unavailable', output)
        self.assertFalse((self.root / 'persistent').exists())


class PostBuildTest(SandboxTest):
    def test_rootfs_installs_runtime_helpers(self):
        self.write('bin/test-arm-gcc', '#!/bin/sh\necho "gcc (fixture) 8.2"\n', executable=True)
        self.write('bin/test-arm-as', '#!/bin/sh\necho "as (fixture) 2.32"\n', executable=True)
        config = self.write('buildroot.config', 'BR2_TOOLCHAIN_EXTERNAL_PREFIX=test-arm\nBR2_TARGET_GENERIC_ROOT_PASSWD=fixture\n')
        for board in BOARDS:
            with self.subTest(board=board):
                target = self.root / board
                for path in ('etc/init.d', 'usr/sbin', 'sbin', 'bin', 'opt', 'lib/mdev',
                             'etc/network/if-up.d', 'etc/network/if-down.d',
                             'etc/network/if-pre-up.d', 'etc/network/if-post-down.d'):
                    (target / path).mkdir(parents=True, exist_ok=True)
                (target / 'etc/inittab').write_text('::sysinit:/bin/hostname -F /etc/hostname\n')
                (target / 'etc/fstab').write_text('')
                self.env.update(BR2_CONFIG=str(config), TARGET_DIR=str(target))
                script = FIRMWARE / f'buildroot/board/{board}/post-build.sh'
                self.run_command(['sh', str(script)])
                library = target / 'usr/lib/antsdr/persistence.sh'
                diagnostic = target / 'usr/sbin/antsdr-diagnostics'
                self.assertEqual(library.read_bytes(), (FIRMWARE / 'buildroot/board/antsdr/persistence.sh').read_bytes())
                self.assertTrue(os.access(diagnostic, os.X_OK))
                info = json.loads((target / 'etc/antsdr-release.json').read_text())
                self.assertEqual(info['board'], board)
                self.assertEqual(info['linux'], '5.2.28')
                self.assertNotIn('#BUILD#', (target / 'etc/motd').read_text())
                self.assertIn(info['source_revision'], (target / 'opt/VERSIONS').read_text())
                for command in ('device_passwd', 'device_persistent_keys'):
                    self.assertTrue(os.access(target / 'usr/sbin' / command, os.X_OK))
