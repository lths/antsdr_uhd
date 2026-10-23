"""Strict-host-key SSH operations and checksummed, inactive-slot deployment."""
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import struct
import time
import zlib

FILES = ('BOOT.bin', 'boot.scr', 'antsdr.bit', 'uImage', 'uEnv.txt',
         'devicetree.dtb', 'uramdisk.image.gz', 'BUILD_INFO.json')
BOOT = '/mnt/antsdr-boot'


def address(value):
    return str(ipaddress.IPv4Address(value))


def discover(host, timeout=1):
    request = struct.pack('!4I32s8s', *map(ord, '1m9j'), b'', b'')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((address(host), 49100))
        start = time.monotonic()
        sock.send(request)
        packet = sock.recv(1024)
    if len(packet) != 56:
        raise ValueError('Malformed discovery response length')
    check, reply, serial_flag, auth, serial, model = struct.unpack('!4I32s8s', packet)
    if (check, reply, serial_flag, auth) != tuple(map(ord, '1M0c')):
        raise ValueError('Malformed discovery response header')
    return {'address':host, 'serial':serial.rstrip(b'\0').decode('ascii'),
            'model':model.rstrip(b'\0').decode('ascii'),
            'rtt_ms':(time.monotonic()-start)*1000}


def verify_release(directory):
    root = Path(directory)
    sums = {}
    for line in (root/'SHA256SUMS').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in sums:
            raise ValueError('Invalid or duplicate checksum entry')
        sums[match[2]] = match[1]
    if set(sums) != set(FILES):
        raise ValueError('Release must contain the complete known boot-file set')
    for filename, digest in sums.items():
        path = root/filename
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Checksum mismatch: '+filename)
    info = json.loads((root/'BUILD_INFO.json').read_text())
    if info.get('board') not in ('e200', 'e310v2') or not re.fullmatch(r'[0-9a-f]{40}', info.get('source_revision', '')):
        raise ValueError('Invalid release identity')
    if info.get('boot_protocol') != 1:
        raise ValueError('Release lacks recoverable-boot protocol support')
    for filename in ('uImage', 'uramdisk.image.gz', 'boot.scr'):
        raw = (root/filename).read_bytes()
        if len(raw) < 64:
            raise ValueError('Truncated image: '+filename)
        magic, header_crc, _, size, _, _, data_crc = struct.unpack('!7I', raw[:28])
        header = raw[:4]+b'\0'*4+raw[8:64]
        if magic != 0x27051956 or len(raw) != 64+size or zlib.crc32(header) != header_crc or zlib.crc32(raw[64:]) != data_crc:
            raise ValueError('Invalid image CRC: '+filename)
    return info


class SSH:
    def __init__(self, host, known_hosts, password=None, identity=None):
        self.host = address(host)
        self.known_hosts = Path(known_hosts)
        self.password = password
        self.identity = identity
        self.client = None

    def __enter__(self):
        import paramiko
        self.client = paramiko.SSHClient()
        self.client.load_host_keys(str(self.known_hosts))
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.client.connect(self.host, username='root', password=self.password,
                            key_filename=self.identity, timeout=5, auth_timeout=8,
                            banner_timeout=5, allow_agent=self.password is None,
                            look_for_keys=self.password is None)
        return self

    def __exit__(self, *args):
        self.client.close()

    def run(self, command, timeout=30, check=True):
        stdin, stdout, stderr = self.client.exec_command('set -eu; '+command, timeout=timeout)
        stdin.close()
        output = stdout.read().decode('utf-8', errors='replace')
        errors = stderr.read().decode('utf-8', errors='replace')
        result = stdout.channel.recv_exit_status()
        if check and result:
            raise RuntimeError('Device command failed ({}): {}'.format(result, errors.strip()))
        return output

    def upload(self, local, remote):
        stdin, stdout, stderr = self.client.exec_command('umask 077; cat > '+shlex.quote(remote), timeout=60)
        with Path(local).open('rb') as stream:
            while True:
                block = stream.read(1024*1024)
                if not block:
                    break
                stdin.write(block)
        stdin.flush()
        stdin.channel.shutdown_write()
        stdout.read()
        error = stderr.read().decode(errors='replace')
        if stdout.channel.recv_exit_status():
            raise RuntimeError('Upload failed: '+error)
        stdin.close()


def trust_console(host, serial_path, known_hosts, password=None):
    """Trust only the public key authenticated over the physical serial console."""
    import paramiko
    import serial
    host = address(host)
    # Dropbear -R creates its host key on the first SSH handshake.
    with socket.create_connection((host,22), timeout=5) as sock:
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=5)
        finally:
            transport.close()
    buffer = ''
    stage = 'initial'
    with serial.Serial(serial_path, 115200, timeout=0.1) as port:
        port.write(b'\r')
        deadline = time.monotonic()+25
        while time.monotonic() < deadline:
            buffer += port.read(65536).decode(errors='replace').replace('\r', '')
            if re.search(r'login:\s*$', buffer):
                if stage not in ('initial',):
                    raise RuntimeError('Console authentication failed')
                port.write(b'root\r'); stage='username'; buffer=''
            elif re.search(r'Password:\s*$', buffer):
                if stage != 'username' or password is None:
                    raise RuntimeError('Console login requires the password supplied through --password-env')
                port.write(password.encode()+b'\r'); stage='password'; buffer=''
            elif stage != 'command' and re.search(r'(?:^|\n)[^\n]*# ?$', buffer):
                port.write(b'dropbearkey -y -f /etc/dropbear/dropbear_ecdsa_host_key; echo __HOST_KEY_DONE__\r')
                stage='command'; buffer=''
            if stage == 'command' and '\n__HOST_KEY_DONE__\n' in buffer:
                match = re.search(r'^ecdsa-sha2-nistp256 ([A-Za-z0-9+/=]+)(?: |$)', buffer, re.M)
                if not match:
                    raise RuntimeError('Console did not return a valid public host key')
                key = paramiko.ECDSAKey(data=base64.b64decode(match[1], validate=True))
                path = Path(known_hosts)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                hosts = paramiko.HostKeys()
                if path.exists(): hosts.load(str(path))
                hosts.add(host, key.get_name(), key)
                hosts.save(str(path)); path.chmod(0o600)
                port.write(b'exit\r')
                return {'host':host, 'key_type':key.get_name(), 'fingerprint_sha256':base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')}
        raise TimeoutError('Timed out verifying the SSH host key over serial')


def health(connection):
    native = connection.run('/usr/sbin/antsdr-health --json', check=False)
    if native:
        current = json.loads(native)
    else:
        # Original fallback images predate the native health utility.
        services = connection.run('/etc/init.d/S90device_start.sh status', check=False).strip() == 'OK'
        try:
            discover(connection.host)
            discovery = True
        except (OSError,ValueError):
            discovery = False
        current = {'schema_version':1,'legacy_health':True,'healthy':services and discovery,
                   'radio_running':services,'network_running':services,'discovery':discovery,
                   'firmware':json.loads(connection.run('cat /etc/antsdr-release.json')),
                   'mac':connection.run('cat /sys/class/net/eth0/address').strip(),
                   'link_up':connection.run('cat /sys/class/net/eth0/carrier').strip()=='1',
                   'uptime_seconds':float(connection.run('cat /proc/uptime').split()[0]),
                   'restarts':0,'maintenance':False}
    cached = connection.run('cat /run/antsdr-health.json 2>/dev/null', check=False)
    if cached:
        supervision = json.loads(cached)
        for field in ('restarts', 'consecutive_failures', 'recovery_limited'):
            current[field] = supervision[field]
    stats = connection.run('ethtool -S eth0 2>/dev/null', check=False)
    current['hardware_counters'] = {key:int(value) for key,value in re.findall(r'^\s+([A-Za-z0-9_]+): (\d+)\s*$', stats, re.M)}
    current['recent_events'] = connection.run("{ logread 2>/dev/null || cat /var/log/messages 2>/dev/null; } | grep -E 'antsdr-health|antsdr-boot' | tail -20", check=False).splitlines()
    command_line = connection.run('cat /proc/cmdline')
    match = re.search(r'(?:^| )antsdr\.slot=(a|b|root)(?: |$)', command_line.strip())
    current['boot_slot'] = match[1] if match else 'legacy'
    current['confirmed_slot'] = connection.run('cat '+BOOT+'/antsdr-active 2>/dev/null', check=False).strip() or 'root'
    current['trial_slot'] = connection.run('cat '+BOOT+'/antsdr-trial 2>/dev/null', check=False).strip() or None
    return current


def deploy(connection, directory):
    info = verify_release(directory)
    running = json.loads(connection.run('cat /etc/antsdr-release.json'))
    if running['board'] != info['board']:
        raise ValueError('Release targets a different board')
    connection.run('mkdir -p '+BOOT+'; mountpoint -q '+BOOT+' || mount -t vfat -o rw,umask=077,nodev,nosuid,noexec /dev/mmcblk0p1 '+BOOT)
    connection.run('test -f '+BOOT+'/BOOT.bin && test -f '+BOOT+'/uImage && test ! -e '+BOOT+'/antsdr-trial')
    active = connection.run('cat '+BOOT+'/antsdr-active 2>/dev/null', check=False).strip()
    if active not in ('', 'a', 'b'):
        raise ValueError('Invalid active-slot marker')
    slot = 'b' if active == 'a' else 'a'
    stage = BOOT+'/antsdr-slots/.'+slot+'-'+info['source_revision']
    final = BOOT+'/antsdr-slots/'+slot
    connection.run('mkdir -p '+BOOT+'/antsdr-slots; mkdir '+stage)
    for filename in (*FILES, 'SHA256SUMS'):
        connection.upload(Path(directory)/filename, stage+'/'+filename)
    connection.run('(cd '+stage+' && sha256sum -c SHA256SUMS)')
    retired = final+'-retired-'+str(time.time_ns())
    connection.run('if test -e '+final+'; then mv '+final+' '+retired+'; fi; mv '+stage+' '+final+'; sync')
    # Install the dispatcher once. The existing FSBL/U-Boot remains untouched.
    protocol = connection.run('cat '+BOOT+'/antsdr-boot-protocol 2>/dev/null', check=False).strip()
    if not protocol:
        connection.run('mkdir -p '+BOOT+'/antsdr-bootstrap-backup; if test ! -f '+BOOT+'/antsdr-bootstrap-backup/uEnv.txt; then cp '+BOOT+'/uEnv.txt '+BOOT+'/antsdr-bootstrap-backup/uEnv.txt; fi')
        for filename in ('boot.scr', 'uEnv.txt'):
            connection.upload(Path(directory)/filename, BOOT+'/'+filename+'.new')
        connection.run('(cmp -s '+BOOT+'/boot.scr.new '+final+'/boot.scr && cmp -s '+BOOT+'/uEnv.txt.new '+final+'/uEnv.txt) || exit 1; sync; mv '+BOOT+'/boot.scr.new '+BOOT+'/boot.scr; mv '+BOOT+'/uEnv.txt.new '+BOOT+'/uEnv.txt; printf 1 > '+BOOT+'/antsdr-boot-protocol; sync')
    elif protocol != '1':
        raise ValueError('Unsupported installed boot protocol')
    connection.run('rm -f '+BOOT+'/antsdr-attempted; printf '+slot+' > '+BOOT+'/antsdr-trial.new; sync; mv '+BOOT+'/antsdr-trial.new '+BOOT+'/antsdr-trial; sync')
    return {'slot':slot, 'previous_slot':active or 'root', 'release':info}


def rollback(connection):
    connection.run('test -f '+BOOT+'/antsdr-boot-protocol')
    pending = connection.run('cat '+BOOT+'/antsdr-trial 2>/dev/null', check=False).strip()
    if pending:
        if pending not in ('a','b'):
            raise ValueError('Invalid pending trial marker')
        active = connection.run('cat '+BOOT+'/antsdr-active 2>/dev/null', check=False).strip() or 'root'
        if active not in ('a','b','root'):
            raise ValueError('Invalid active slot marker')
        connection.run('rm -f '+BOOT+'/antsdr-trial '+BOOT+'/antsdr-attempted; sync')
        return {'selected_slot':active,'cancelled_trial':pending}
    previous = connection.run('cat '+BOOT+'/antsdr-previous 2>/dev/null', check=False).strip()
    if previous not in ('', 'root', 'a', 'b'):
        raise ValueError('Invalid previous-slot marker')
    if previous in ('a', 'b'):
        connection.run('printf '+previous+' > '+BOOT+'/antsdr-active.new; sync; mv '+BOOT+'/antsdr-active.new '+BOOT+'/antsdr-active')
    elif previous == 'root':
        connection.run('rm -f '+BOOT+'/antsdr-active')
    connection.run('rm -f '+BOOT+'/antsdr-trial '+BOOT+'/antsdr-attempted; sync')
    return {'selected_slot':previous or 'current active slot'}
