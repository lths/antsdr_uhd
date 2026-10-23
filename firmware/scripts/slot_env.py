#!/usr/bin/env python3
"""Add the recoverable boot dispatcher to an extracted U-Boot environment."""
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
result = []
for line in lines:
    if line.startswith('sdboot='):
        result.append('sdboot_legacy='+line.partition('=')[2])
        result.append('sdboot=if fatload mmc 0 ${antsdr_script_addr} boot.scr; then source ${antsdr_script_addr}; else run sdboot_legacy; fi')
    else:
        result.append(line)
result += ['antsdr_script_addr=0x1f00000', 'antsdr_state_addr=0x1e00000']
path.write_text('\n'.join(result)+'\n')
