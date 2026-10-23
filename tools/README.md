# E200 operations and receive workspace

The tools provide device health, verified inactive-slot deployment, profiles,
calibration records, receive benchmarks, triggered I/Q capture, spectrum, and a
waterfall dashboard. Radio operations receive only. The dashboard runs on this
computer at `http://127.0.0.1:8088`; it does not install a web server on the E200.

## Build once

From the repository root, with Docker available:

```sh
docker build -t antsdr-host:22.04 host/docker
docker run --rm -v "$PWD:/work" antsdr-host:22.04 bash host/docker/build.sh
tools/run discover
```

The container contains a compatible Python API and host library. Host build
outputs remain under `host/build-antsdr/`. Named profiles, calibration records,
captures, and the device's verified public host key are stored under `.antsdr/`
by default. `--state` selects a different directory mounted inside `/work`.

## Establish device trust and open the dashboard

The first migration requires console access to verify the SSH host key. Supply
the existing board password through an environment variable; it is never put in
command-line arguments, committed files, or capture metadata:

```sh
read -rsp 'E200 password: ' ANTSDR_PASSWORD; echo
export ANTSDR_PASSWORD
tools/run --serial /dev/ttyUSB0 --password-env ANTSDR_PASSWORD trust-console
tools/run --password-env ANTSDR_PASSWORD health
tools/run --password-env ANTSDR_PASSWORD serve
```

A fresh SD installation creates a new SSH host key and a MAC derived from that
card. Run `trust-console` again after swapping cards; keep the old card as a
fallback. Failed SSH setup closes its connection immediately, including host-key
and authentication failures, so dashboard retries do not occupy the device's
unauthenticated connection slots.

The serial port must be accessible to UID 1000. After provisioning persistent
authorized keys, `--identity /work/path/to/private-key` or an available SSH agent
can replace password authentication. Private keys must be provided explicitly
to the container; the wrapper does not mount your home or SSH agent by default.
The dashboard requires a same-origin session token for changes and listens only
on loopback. It permits one receive session at a time and reports competing
radio ownership errors.

## Profiles and capture

The default `e200-rx` profile uses 1 GHz, 1 MS/s, 1 MHz bandwidth, gain 10 dB,
channel 0, RX2, and the internal clock. Existing profiles are never silently
replaced when defaults change. Inspect or import a JSON profile:

```sh
tools/run profile list
tools/run profile put /work/my-profile.json
tools/run benchmark --rates 1000000 5000000 10000000 --duration 5 \
    --report firmware/build/validation/rx-benchmark.json
tools/run capture --duration 2 --output .antsdr/captures/immediate.cf32
tools/run capture --duration 2 --trigger-after 1 --pretrigger-seconds 0.1 \
    --output .antsdr/captures/delayed.cf32
tools/run capture --duration 2 --threshold-dbfs -45 --trigger-timeout 30 \
    --pretrigger-seconds 0.1 --output .antsdr/captures/level.cf32
```

A level trigger measures RMS amplitude on the first selected channel, once per
received block. Delayed triggers use the host's monotonic clock; they are not
sample-accurate hardware triggers. Pretrigger memory is capped at 256 MiB and
cleared on an overflow. Trigger wait time and receive duration are bounded.

Files contain little-endian complex float32, interleaved by sample then channel.
Each `.cf32.json` sidecar records requested and actual settings, device serial,
device sample timestamps, trigger details, calibration, errors and discontinuities.
Interrupted or untriggered captures have `complete: false`. Destination files are
never overwritten. For two channels, reshape loaded data to `(-1, 2)`.
Two-channel sessions synchronize both device timekeepers after final MIMO clock
and streamer setup. The driver acknowledges arming on each radio before
pulsing their shared synchronization line; the sidecar records that the device
time origin was reset. The FPGA also resets halfband decimation phase while
idle so different previous channel histories do not offset the next dual start.

The dashboard can edit profiles, select a profile, preview a spectrum/waterfall,
and record immediate, delayed or level-triggered captures. Saved files remain in
the local state directory. Spectrum levels use dBFS; they are not absolute dBm.

## Calibration and persistent settings

Calibration records require a real measurement reference, timezone-bearing date,
device serial, frequency error in ppm and a relative level correction in dB.
Import a record with `tools/run calibration put /work/measurement.json`, then
set a profile's `calibration` field to the record's name. A mismatched serial is
rejected. Positive measured frequency error corrects nominal tuning by dividing
the requested frequency by `1 + ppm/1e6`. Level corrections affect spectrum
display, while raw recorded samples remain unchanged. This does not calibrate
sample-clock rate, absolute RF power or inter-channel phase.

```sh
tools/run calibration list
tools/run --password-env ANTSDR_PASSWORD sync-settings
tools/run --password-env ANTSDR_PASSWORD pull-settings
```

Settings sync validates records, uploads them to `/etc/antsdr/`, and publishes a
checksummed snapshot in private device storage. The E200 uses a 64 MiB ext4 image
`antsdr-state.ext4` on the existing FAT partition, mounted at `/mnt/antsdr-state`.
This preserves the SD partition table. A working existing JFFS2 store retains
priority. Settings and SSH keys have Unix permissions inside the private
filesystem; the storage image is not encrypted. Startup waits for the card and
retries its unmounted controller once if Linux cannot enumerate it after a warm
reset. Health exposes `boot_storage_ready`, `persistent_storage_ready` and
`settings_restored`; missing storage prevents successful trial confirmation and
automatic saving of temporary SSH keys. The initial automatic MAC is
derived from the SD card CID and persisted; moving to a different card requires
copying the settings image to retain the identity. It is not a factory MAC.

An optional `/etc/antsdr/network.conf` accepts only `hostname`, `address`, and
`netmask` entries, one `key=value` per line. An empty address selects DHCP.
`antsdr-config save` persists it. Invalid values are rejected before network
activation. `device_passwd` and `device_persistent_keys` use the same storage.

## Deployment and recovery

```sh
tools/run verify firmware/build_sdimg
tools/run --serial /dev/ttyUSB0 --password-env ANTSDR_PASSWORD \
    deploy firmware/build_sdimg --reboot
tools/run --serial /dev/ttyUSB0 --password-env ANTSDR_PASSWORD rollback --reboot
```

`firmware/scripts/release.sh e200 --serial /dev/ttyUSB0 --password-env ANTSDR_PASSWORD`
builds and deploys in one invocation. Set `XILINX_ROOT` and `JOBS` when needed.
It requires the previously prepared `antsdr-build:2019.1` image.

Deployment verifies the local manifest and legacy image CRCs, checks the board
model, uploads the inactive slot, and verifies every uploaded file before
publishing a trial marker. The original root boot files remain a fallback. The
existing BOOT.bin/FSBL/U-Boot is retained; the first migration installs only an
SD boot dispatcher and a backed-up `uEnv.txt`. Updates select fixed `a`/`b` slots.
Retired inactive slots are retained for inspection rather than deleted.

The bootloader writes an attempt marker before a trial. An attempted but
unconfirmed trial falls back on the next boot; image-load failures fall back
immediately. Linux confirms the slot only after service health, image checksums,
and the running build identity pass. Failed service/integrity checks request a
reboot. Kernel panic reboot is set to ten seconds. A hard hang before the Linux
watchdog starts still needs a reset/power cycle; the attempt marker then selects
the previous image. The dispatcher cannot recover damaged SD hardware or a
broken FSBL/U-Boot, which normal deployment does not replace.

The health supervisor checks process identity and the local discovery response.
With link up, three consecutive failures request a service restart. Recovery is
capped at three attempts per ten minutes and logged through syslog. Explicit
service stops enter maintenance mode. Health failures do not cause continuous
whole-board reboot loops. The hardware watchdog separately covers OS stalls.

## Tests and CI

```sh
python3 -m unittest discover -s firmware/tests -v
PYTHONPATH=tools python3 -m unittest discover -s tools/tests -v
docker run --rm -v "$PWD:/work" antsdr-host:22.04 bash host/docker/build.sh
```

GitHub Actions runs hardware-free firmware tests and the host build/tool tests.
The manually dispatched firmware workflow requires a trusted self-hosted runner
labelled `antsdr-xilinx-2019.1` with the licensed tools already installed. Neither
workflow automatically deploys to a connected radio.
