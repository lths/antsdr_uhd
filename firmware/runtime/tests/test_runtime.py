"""Exercise real UDP discovery and signal shutdown without FPGA access."""
import hashlib
import json
import tempfile
import time
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

    def test_lease_contention_tokens_retries_expiry_and_diagnostics(self):
        binary = Path(os.environ.get('ANTSDR_RUNTIME', 'firmware/build/runtime-native/e200')).resolve()
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen([str(binary), '--no-hardware', '--bind', '127.0.0.1',
                '--port', '0', '--lease-port', '0', '--dna', '123', '--status-dir', directory],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = process.stdout.readline()
                port = int(line.split('lease_port=')[1].split()[0])
                sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
                for sock, address in zip(sockets, ('127.0.0.1', '127.0.0.2')):
                    self.addCleanup(sock.close); sock.bind((address, 0))
                    sock.connect(('127.0.0.1', port)); sock.settimeout(.5)
                def exchange(sock, op, nonce, token=0, reserved=0):
                    sock.send(struct.pack('!8I', 0x414c5331, 1, op, nonce,
                                          token >> 32, token & 0xffffffff, reserved, 0))
                    reply = struct.unpack('!8I', sock.recv(128))
                    self.assertEqual(reply[:4], (0x414c5331, 1, op, nonce))
                    return reply
                a, b = sockets
                grant = exchange(a, 1, 100)
                self.assertEqual(grant[6], 0)
                token = grant[4] << 32 | grant[5]
                self.assertNotEqual(token, 0)
                retry = exchange(a, 1, 100)
                self.assertEqual(retry[4:6], grant[4:6])
                self.assertLessEqual(retry[7], grant[7])
                for sock in sockets:
                    busy = exchange(sock, 1, 101)
                    self.assertEqual(busy[4:7], (0, 0, 1))
                self.assertEqual(exchange(b, 2, 102, token)[6], 2)
                self.assertEqual(exchange(a, 2, 103, token ^ 1)[6], 2)
                self.assertEqual(exchange(a, 2, 104, token, reserved=1)[6], 3)
                self.assertEqual(exchange(a, 2, 105, token)[6], 0)
                time.sleep(3.2)
                self.assertEqual(exchange(a, 2, 106, token)[6], 2)
                # New ownership is granted only after the close/flush interval.
                time.sleep(.06)
                next_grant = exchange(b, 1, 107)
                self.assertEqual(next_grant[6], 0)
                next_token = next_grant[4] << 32 | next_grant[5]
                self.assertNotEqual(next_token, token)
                self.assertEqual(exchange(a, 3, 108, token)[6], 2)
                self.assertEqual(exchange(b, 3, 109, next_token)[6], 0)
                time.sleep(.15)
                report = json.loads((Path(directory)/'status.json').read_text())
                self.assertEqual(report['leases_acquired'], 2)
                self.assertEqual(report['leases_expired'], 1)
                self.assertEqual(report['fpga']['lease_remaining_ms'], 0)
                self.assertNotIn('token', json.dumps(report))
                a.send(struct.pack('!8I', 0x414c5331, 2, 1, 200, 0, 0, 0, 0))
                with self.assertRaises(socket.timeout): a.recv(128)
            finally:
                process.terminate(); _, stderr = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 0, stderr)

    def test_test_identity_cannot_override_real_hardware(self):
        binary = Path(os.environ.get('ANTSDR_RUNTIME', 'firmware/build/runtime-native/e200')).resolve()
        for args in (['--dna', '123'], ['--no-hardware', '--dna', '123'],
                     ['--no-hardware', '--bind', '127.0.0.1', '--dna', '-1']):
            self.assertEqual(subprocess.run([str(binary), *args], capture_output=True).returncode, 2)


if __name__ == '__main__':
    unittest.main()
