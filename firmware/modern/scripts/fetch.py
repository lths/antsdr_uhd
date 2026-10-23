#!/usr/bin/env python3
"""Fetch checksum-pinned sources; extracted trees and downloads are build outputs."""
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request


def main():
    modern = Path(__file__).resolve().parents[1]
    base = modern.parent / 'build/modern'
    for item in json.loads((modern / 'sources.lock.json').read_text()).values():
        archive = base / 'sources' / item['url'].rsplit('/', 1)[1]
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            partial = archive.with_name(archive.name+'.partial')
            with urllib.request.urlopen(item['url'], timeout=60) as response, partial.open('wb') as output:
                while True:
                    block = response.read(1024*1024)
                    if not block: break
                    output.write(block)
            partial.replace(archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != item['sha256']:
            raise ValueError('Source checksum mismatch: '+str(archive))
        name = archive.name.split('.tar.')[0]
        target = base / 'src' / name
        stamp = target / '.antsdr-source-sha256'
        if not stamp.exists() or stamp.read_text().strip() != digest:
            target.mkdir(parents=True, exist_ok=True)
            # These pinned upstream archives contain intentional absolute rootfs
            # symlinks. GNU tar preserves those while stripping the top directory.
            subprocess.run(['tar', '--no-same-owner', '--strip-components=1', '-xf', str(archive), '-C', str(target)], check=True)
            stamp.write_text(digest+'\n')
        print(name+' '+digest, flush=True)


if __name__ == '__main__':
    main()
