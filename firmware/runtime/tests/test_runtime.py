"""Exercise real UDP discovery and signal shutdown without FPGA access."""
import hashlib
import os
from pathlib import Path
import socket
import struct
import subprocess
import unittest


class DiscoveryTest(unittest.TestCase):
    def test_wire_protocol_and_shutdown(self):
        binary = Path(os.environ.get('ANTSDR_RUNTIME', 'firmware/build/runtime-native/e200')).resolve()
        process = subprocess.Popen([str(binary), '--no-hardware', '--bind', '127.0.0.1',
                                    '--port', '0', '--dna', '123456789abcdef'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = process.stdout.readline()
            port = int(line.split('port=')[1].split()[0])
            expected = hashlib.md5(b'123456789abcdef').hexdigest().encode()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(('127.0.0.1', port)); sock.settimeout(.1)
                header = struct.pack('!4I', ord('1'), ord('m'), ord('9'), ord('j'))
                for size in (48, 56):
                    sock.send(header + bytes(size-16))
                    response = sock.recv(2048)
                    self.assertEqual(len(response), 56)
                    self.assertEqual(struct.unpack('!4I', response[:16]), (49, 77, 48, 99))
                    self.assertEqual(response[16:48], expected)
                    self.assertEqual(response[48:], b'E200  v1')
                for packet in (b'', header, header+bytes(39), header+bytes(41),
                               bytes(4)+header[4:]+bytes(40), header+bytes(4000)):
                    sock.send(packet)
                    with self.assertRaises(socket.timeout): sock.recv(2048)
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=2)
        self.assertEqual(process.returncode, 0, stderr)

    def test_test_identity_cannot_override_real_hardware(self):
        binary = Path(os.environ.get('ANTSDR_RUNTIME', 'firmware/build/runtime-native/e200')).resolve()
        for args in (['--dna', '123'], ['--no-hardware', '--dna', '123'],
                     ['--no-hardware', '--bind', '127.0.0.1', '--dna', '-1']):
            self.assertEqual(subprocess.run([str(binary), *args], capture_output=True).returncode, 2)


if __name__ == '__main__':
    unittest.main()
