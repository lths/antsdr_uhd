"""Verify packaged provenance against an isolated source repository."""

import json
import shutil
import subprocess

from test_firmware import FIRMWARE, SandboxTest


class BuildInfoTest(SandboxTest):
    def setUp(self):
        super().setUp()
        self.source = self.root / 'source'
        for component in ('linux', 'u-boot-xlnx', 'buildroot'):
            dest = self.source / component / 'Makefile'
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIRMWARE / component / 'Makefile', dest)
        self.target = self.root / 'target'
        self.write('target/etc/motd', 'Firmware #BUILD#\n')
        self.write('bin/test-gcc', '#!/bin/sh\necho "test-gcc (fixture) 8.2"\n', executable=True)
        self.env['SOURCE_DATE_EPOCH'] = '1600000000'

    def generate(self, board='e200'):
        self.run_command(['python3', str(FIRMWARE / 'scripts/build_info.py'),
                          '--source', str(self.source), '--target-dir', str(self.target),
                          '--board', board, '--compiler', 'test-gcc'])
        return json.loads((self.target / 'etc/antsdr-release.json').read_text())

    def test_source_archive_has_explicit_unknown_identity(self):
        info = self.generate('e310v2')
        self.assertEqual(info['source_revision'], 'unknown')
        self.assertIsNone(info['source_dirty'])
        self.assertEqual(info['board'], 'e310v2')
        self.assertEqual(info['linux'], '5.2.28')
        self.assertEqual(info['u_boot'], '2016.07')
        self.assertEqual(info['buildroot'], '2020.02.8')
        self.assertEqual(info['compiler'], 'test-gcc (fixture) 8.2')
        self.assertEqual(info['build_time_utc'], '2020-09-13T12:26:40Z')
        self.assertNotIn('#BUILD#', (self.target / 'etc/motd').read_text())
        before = (self.target / 'etc/antsdr-release.json').read_bytes()
        self.generate('e310v2')
        self.assertEqual(before, (self.target / 'etc/antsdr-release.json').read_bytes())

    def test_clean_modified_and_untracked_sources_are_distinguished(self):
        self.run_command(['git', 'init', str(self.source)])
        self.run_command(['git', '-C', str(self.source), 'add', '.'])
        self.run_command(['git', '-C', str(self.source), '-c', 'user.name=Fixture',
                          '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture'])
        revision = subprocess.check_output(['git', '-C', str(self.source), 'rev-parse', 'HEAD'], text=True).strip()
        info = self.generate()
        self.assertEqual(info['source_revision'], revision)
        self.assertIs(info['source_dirty'], False)
        self.assertIn(revision + ' (clean)', (self.target / 'opt/VERSIONS').read_text())
        self.write('source/untracked', 'new input')
        self.assertIs(self.generate()['source_dirty'], True)
        (self.source / 'untracked').unlink()
        with (self.source / 'linux/Makefile').open('a') as stream:
            stream.write('\n# modified input\n')
        self.assertIs(self.generate()['source_dirty'], True)
        self.assertIn(revision + ' (dirty)', (self.target / 'opt/VERSIONS').read_text())

    def test_failed_compiler_does_not_publish_identity(self):
        self.write('bin/test-gcc', '#!/bin/sh\nexit 1\n', executable=True)
        self.run_command(['python3', str(FIRMWARE / 'scripts/build_info.py'),
                          '--source', str(self.source), '--target-dir', str(self.target),
                          '--board', 'e200', '--compiler', 'test-gcc'], success=False)
        self.assertFalse((self.target / 'etc/antsdr-release.json').exists())
