#!/usr/bin/env python3
"""Generate the identity installed in the rootfs, without running target code."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


def git(source, *args):
    return subprocess.check_output(
        ['git', '-C', str(source), *args], stderr=subprocess.DEVNULL,
        universal_newlines=True).strip()


def make_version(path, keys):
    text = path.read_text()
    values = []
    for key in keys:
        match = re.search(r'^(?:export[ \t]+)?' + key + r'[ \t]*[:?]?=[ \t]*([^\n#]*)', text, re.M)
        if not match:
            raise ValueError('Missing {} in {}'.format(key, path))
        if match.group(1).strip():
            values.append(match.group(1).strip())
    return '.'.join(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--board', required=True, choices=('e200', 'e310v2'))
    parser.add_argument('--target-dir', type=Path, required=True)
    parser.add_argument('--compiler', required=True)
    args = parser.parse_args()

    revision, dirty = 'unknown', None
    try:
        revision = git(args.source, 'rev-parse', 'HEAD')
        dirty = bool(git(args.source, 'status', '--porcelain', '--untracked-files=normal'))
    except (OSError, subprocess.CalledProcessError):
        pass  # Source archives can be built without a .git directory.
    epoch = os.environ.get('SOURCE_DATE_EPOCH')
    built = (datetime.fromtimestamp(int(epoch), timezone.utc) if epoch is not None
             else datetime.now(timezone.utc))
    compiler = subprocess.check_output([args.compiler, '--version'], universal_newlines=True).splitlines()[0]
    info = {
        'schema_version': 1,
        'boot_protocol': 1,
        'board': args.board,
        'source_revision': revision,
        'source_dirty': dirty,
        'build_time_utc': built.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'compiler': compiler,
        'linux': make_version(args.source / 'linux/Makefile', ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')),
        'u_boot': make_version(args.source / 'u-boot-xlnx/Makefile', ('VERSION', 'PATCHLEVEL', 'SUBLEVEL')),
        'buildroot': make_version(args.source / 'buildroot/Makefile', ('BR2_VERSION',)),
    }
    state = {True: 'dirty', False: 'clean', None: 'unknown'}[dirty]
    identity = 'ANTSDR {} {} ({})'.format(args.board.upper(), revision, state)
    versions = '\n'.join([
        identity,
        'built ' + info['build_time_utc'],
        'linux ' + info['linux'],
        'u-boot ' + info['u_boot'],
        'buildroot ' + info['buildroot'],
        'compiler ' + compiler,
        '',
    ])
    for name, content in (
            ('etc/antsdr-release.json', json.dumps(info, indent=2, sort_keys=True) + '\n'),
            ('opt/VERSIONS', versions)):
        path = args.target_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o644)
    banner = args.target_dir / 'etc/motd'
    if banner.exists():
        banner.write_text(banner.read_text().replace('#BUILD#', identity))


if __name__ == '__main__':
    main()
