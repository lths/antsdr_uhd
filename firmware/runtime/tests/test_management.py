"""Exercise the production readback validator without MMIO or root privileges."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


class CompatibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='antsdr-compat-')
        cls.root = Path(cls.temp.name)
        runtime = Path(__file__).resolve().parents[1]
        harness = cls.root / 'check.c'
        harness.write_text('#define main runtime_main\n#include "radio.c"\n#undef main\n'
            'int main(int argc, char **argv) {\n'
            ' if (argc!=2) return 2; status_fd=open(argv[1],O_RDONLY);\n'
            ' struct registers r=read_registers(); return r.magic!=ANTSDR_MGMT_MAGIC;\n}\n')
        cls.binary = cls.root / 'check'
        subprocess.run([os.environ.get('CC', 'gcc'), '-std=c11', '-I', str(runtime),
            str(harness), str(runtime/'md5.c'), '-o', str(cls.binary)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_incompatible_or_malformed_readback_fails_closed(self):
        status = self.root / 'status'
        for text in ('414d4731 00010000 c0a8010a 1 00000000 0 0\n',
                     'deadbeef 00010000 c0a8010a 1 00000000 0 0\n',
                     '414d4731 00020000 c0a8010a 1 00000000 0 0\n',
                     '414d4731 00010000 truncated\n', ''):
            status.write_text(text)
            result = subprocess.run([str(self.binary), str(status)], capture_output=True)
            self.assertEqual(result.returncode == 0, text.startswith('414d4731 00010000 c0a8010a'))
