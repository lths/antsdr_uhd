"""Create a separate Gqrx E200 profile without modifying existing settings."""
from pathlib import Path
import sys

from PyQt5.QtCore import QSettings

path = Path(sys.argv[1])
if not path.exists():
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings = QSettings(str(path), QSettings.IniFormat)
    for key, value in {
        'configversion': 2,
        'input/device': 'uhd=0,type=ant,addr=192.168.1.10',
        'input/sample_rate': 1000000,
        'input/decimation': 1,
        'input/bandwidth': 1000000,
        'input/antenna': 'RX2',
        'input/lnb_lo': 0,
        'input/gains': {'PGA': 100},  # Gqrx stores gain settings multiplied by ten.
        'input/hwagc': False,
        'input/frequency': 902050000,
        'remote_control/enabled': False,
    }.items():
        settings.setValue(key, value)
    settings.sync()
    if settings.status() != QSettings.NoError:
        raise RuntimeError('Could not save Gqrx configuration')
    path.chmod(0o600)
