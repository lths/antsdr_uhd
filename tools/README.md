# E200 operations and radio workspace

The tools provide device health, verified inactive-slot deployment, profiles,
calibration records, receive benchmarks, triggered I/Q capture, spectrum,
waterfall, and finite transmit controls. The dashboard runs on this
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

## Gqrx receiver

After building the host driver above, launch from a terminal on your graphical
desktop:

```sh
tools/gqrx
```

The first launch builds `antsdr-gqrx:22.04` with Ubuntu 22.04 Gqrx and GNU Radio
linked to this repository's UHD 4.1 library. A system Gqrx built against another
UHD ABI, such as UHD 4.9, cannot use that library directly. The launcher uses
X11/XWayland and the desktop's PulseAudio-compatible socket for sound. System
packages and existing Gqrx profiles are unchanged. Docker must be available to
your user; launch from the desktop rather than through `sudo`.

The separate profile is saved at `.antsdr/gqrx/e200.conf`. Initial settings are:

| Setting | Value |
| --- | --- |
| Device string | `uhd=0,type=ant,addr=192.168.1.10` |
| Input sample rate | `1000000` samples/s |
| Decimation | `1` |
| Analog bandwidth | `1000000` Hz |
| Antenna | `RX2` |
| Hardware gain | Manual, `10` dB |
| LNB LO | `0` |
| Initial receive frequency | `902.050` MHz |

Click the top-left start button or press **Ctrl+D** to receive. Enter the desired
frequency and choose its demodulation mode in Receiver Options; for an FM
broadcast station, use **WFM (mono)** or **WFM (stereo)**. Hardware gain is in
Input controls; the Audio gain slider controls speaker volume. Attach the receive
antenna to RX2. Gqrx provides receive controls and does not transmit.

Saved settings persist across launches. Close Gqrx before using dashboard radio
jobs or another UHD application: the device permits one radio owner at a time.
No firmware reflash is required. To check connectivity without opening the GUI,
run `tools/gqrx --probe`; `tools/gqrx --build` builds only the container image.

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
on loopback. It permits one receive or transmit job at a time and reports competing
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

Hardware modes use the trigger-enabled E200 FPGA and an exact finite sample
count. They cannot combine with host delay, host RMS triggering, or host
pretrigger history:

```sh
tools/run capture --trigger-mode device-time --device-delay 0.5 --duration 0.1 \
    --output .antsdr/captures/timed.cf32
tools/run capture --trigger-mode fpga-level --threshold-dbfs -40 --duration 0.1 \
    --output .antsdr/captures/fpga-level.cf32
tools/run capture --trigger-mode external-edge --trigger-timeout 30 --duration 0.1 \
    --output .antsdr/captures/pps.cf32
```

Device delay schedules the start against the current device clock, after final
stream setup. FPGA level mode compares each post-DDC sample's peak `|I|` or
`|Q|` with a 16-bit threshold; it supports one selected channel. The triggering
sample and its timestamp are retained. External mode latches a rising edge on
the external PPS input until the next sample; pulses that occurred before
arming do not fire a new capture. Verify the connector's electrical limits
before wiring a source. An untriggered finite capture reports `complete: false`.
Old FPGA images reject hardware level/edge modes explicitly.

Physical PPS input and single/dual-channel edge triggering have been verified on
the AD9361 E200 with a GPS-locked Leo Bodnar LBE-1421. Select **1 PPS on Output 1**
in the GPSDO configuration; its default 10 MHz clock causes repeated external
edges rather than one pulse per second. In this test, the LBE-1421 emitted no PPS
until it acquired GPS lock. Position its GPS antenna with a clear sky view.

With Gqrx and other radio clients closed, repeat the receive-only check in the
host container:

```sh
docker run --rm --network host --user "$(id -u):$(id -g)" \
    -v "$PWD:/work" antsdr-host:22.04 python3 host/tests/ant_pps_input.py \
    --report firmware/build/validation/pps-input.json
```

The check rejects a disabled/missing or fast external signal, verifies the PPS
time latch and synchronization, and captures three finite bursts on RX0, RX1,
and both channels. Each burst contains 25,000 samples per channel at 1 MS/s.
The report destination must be new. It restores the original time source after
testing and sends no transmit samples. PPS synchronization sets a relative time
origin; UTC numbering and disciplining the RF/sample clock require separate
configuration. The sample clock remains internal during this check.

Files contain little-endian complex float32, interleaved by sample then channel.
Each `.cf32.json` sidecar records requested and actual settings, device serial,
device sample timestamps, trigger details, calibration, errors and discontinuities.
Cancelled or untriggered captures, captures with receive overflows, and captures
that fail while receiving, stopping, or closing their output have `complete: false`.
Reports include cancellation state and error details when an exception occurs.
Destination files are never overwritten. For two channels, reshape loaded data
to `(-1, 2)`.
Two-channel sessions synchronize both device timekeepers after final MIMO clock
and streamer setup. The driver acknowledges arming on each radio before
pulsing their shared synchronization line; the sidecar records that the device
time origin was reset. The FPGA also resets halfband decimation phase while
idle so different previous channel histories do not offset the next dual start.

The dashboard can edit profiles, select a profile, preview a spectrum/waterfall,
and record host- or hardware-triggered captures. Saved files remain in
the local state directory. Spectrum levels use dBFS; they are not absolute dBm.

The transmit panel generates finite tones, symmetric two-tone signals, or linear
chirps. It exposes channel, center frequency, sample rate, bandwidth, gain,
digital amplitude, duration and timed-start delay. Bursts are bounded to two
seconds and two million samples. Transmit and receive jobs share one exclusive
radio owner. Stop ends the sample feed; queued samples may finish. Reports in
`.antsdr/transmit/` record actual settings, sent sample count and asynchronous
events. Completion requires the FPGA burst acknowledgement; errors do not report
success. The transmitter restores minimum gain after completion or cancellation.

## AD9361 receiver tuning

The Receiver tuning panel edits the selected profile through dedicated fields.
Save receiver tuning before starting the next capture or spectrum session. Gain
mode (`manual`, `slow`, `fast`), manual gain setting and antenna can differ by
channel. Frequency, analog bandwidth, LO offset and DC/IQ tracking are shared.
Existing profiles retain their previous defaults: manual gain, tracking enabled,
zero LO offset, and the standard device filter. Updating a saved profile does
not retune a running capture.

```json
{
  "schema_version": 1,
  "name": "dual-agc",
  "frequency_hz": 902050000,
  "sample_rate": 1000000,
  "bandwidth_hz": 2000000,
  "gain_db": 10,
  "channels": [0, 1],
  "antenna": "RX2",
  "gain_mode": "slow",
  "channel_settings": {"1": {"gain_mode": "manual", "gain_db": 23}},
  "auto_dc_offset": true,
  "auto_iq_balance": true,
  "lo_offset_hz": 100000,
  "settle_seconds": 0.05
}
```

Manual gain and `gain_index` use the existing UHD driver's 0–76 gain-table-index
scale. Hardware readback follows AGC changes; it is not a calibrated RF gain in
dB. RSSI is relative dB, and temperature uses the driver's approximate sensor
calibration. Absolute input power in dBm requires bench calibration. Fast AGC
state 5 indicates locked gain. Overload fields are instantaneous hardware flags,
not latched event counts. Sample clipping separately counts received complex
samples with either I or Q at the sc16 full-scale boundary; it cannot diagnose
every analog overload.

LO offset separates the RF synthesizer frequency from the desired digital
center frequency. The requested sample band plus offset must fit within the
analog bandwidth. Setup waits up to two seconds for LO lock, then applies the
selected settling delay. Controls and custom taps are applied after final MIMO
clock setup. Telemetry uses the same owner in a background thread, rather than
opening a competing radio session, and accompanies spectrum frames and capture
reports. Hardware sensor reads release Python's GIL so receiving can continue.

Inspect the actual filter chain, limits and telemetry without receiving or
transmitting samples:

```sh
tools/run radio-info --profile dual-agc
```

The dashboard's Inspect radio and filters action performs the same exclusive
configuration/inspection job. Advanced profiles may include `fir_taps`: 16–128
signed int16 coefficients in multiples of 16, with exactly the active filter's
tap count. The driver preserves its configured clock, interpolation/decimation
and FIR gain. Both selected channels are checked before coefficients are
written; readback must match. A new device session restores standard filters
unless the profile requests custom taps. Filter coefficients can change gain
and alias rejection; the inspector reports the active filters and their rates.

These features require the rebuilt host driver and Python API; no SD or FPGA
update is required. The connected E200's AD9361 variant was confirmed by its
owner; the generic driver name and board EEPROM do not identify RF chip bins.
Device-advertised limits do not establish sustained Ethernet throughput or RF
performance outside the part's specifications.

Run the receive-only hardware regression in the host container:

```sh
python3 host/tests/ant_receiver_controls.py \
  --report firmware/build/validation/receiver-controls.json
```

RX fast-lock scanning is available through the timing and measurement tools
below. TX fast-lock, hybrid/pin-synchronized AGC, TDD state scheduling, arbitrary
gain-table uploads, and a public recalibration API remain separate extensions.
External LO and TX-monitor features also need board routing verification.

## Timing, recordings and measurements

Run `tools/gpsdo` in another terminal while using the dashboard or CLI. It
auto-detects exactly one LBE-1421 and reads its HID status and USB NMEA stream
inside an isolated container. It has no network access or Linux capabilities;
only that device's nodes and the timing-status directory are writable. Status
contains satellite count and UTC, excluding position coordinates. The dashboard
does not require root and does not acquire a second radio owner to read PPS.
Use `tools/gpsdo --status` for an immediate hardware status check. If replugged,
restart the bridge to select the new device nodes; stale state is marked unavailable.

The GPS/PPS panel shows source lock, Output 1 mode, PPS age, missed pulses,
time discontinuities, and UTC association. Board PPS is measured through the
active radio session; an idle dashboard labels its last observation accordingly.
The short-term local clock-offset estimate is available only with external PPS
and a GPS-locked source. It is diagnostic data, not an automatic calibration.

To require GPS-derived sample timestamps, save a receive profile with
`"time_source": "external"` and `"utc_timestamps": true`. The dashboard exposes
both controls. Initialization waits for two consecutive matching PPS/NMEA epochs;
ambiguous, delayed, invalid or stale NMEA messages do not produce UTC tags.
The LBE-1421's observed RMC timestamp is halfway through the second; association
uses its integral UTC second and accepts at most 0.7 seconds of arrival delay
from the observed PPS. Re-establishing the association requires two consecutive
epochs. UTC loss during a required-UTC capture marks it incomplete even if lock
returns before the capture ends. This is a software epoch association, not a
calibration of absolute antenna-to-ADC timing. PPS cable delay and local-clock
interpolation error are not removed. Host `started_at_utc` and frame wall times
remain separate from `first_sample_utc` / `first_saved_sample_utc`.

```sh
tools/run gps-status
tools/run timing --profile gps-utc-rx --duration 10 --report .antsdr/timing-check.json
tools/run capture --profile gps-utc-rx --duration 1 --output .antsdr/captures/utc.cf32
tools/run sigmf --input .antsdr/captures/utc.cf32 --prefix .antsdr/exports/utc
```

Create `gps-utc-rx` through the profile editor; existing profiles are preserved.
The SigMF export writes private `.sigmf-data` and `.sigmf-meta` files, retains
channel interleaving, verifies length, and records a SHA-512 digest. It includes
GPS UTC only when associated, and retains incompleteness/errors/discontinuities
in an optional `antsdr` namespace. Existing destinations are never overwritten.
The dashboard can also export each new capture automatically. Exports conform
to the [SigMF v1.2.6 schema](https://github.com/sigmf/SigMF/blob/v1.2.6/sigmf-schema.json).

The E200 shares one PPS/10 MHz reference connector. Select one external input
mode at a time; the profile validator rejects external clock plus external PPS.
`clock_source: external` now requires three consecutive reference-lock readings
before RX begins, with `reference_timeout_seconds` (default 30, maximum 120).
Loss of reference lock invalidates the capture. The LBE-1421's Output 1 clock
must already be 10 MHz for the following helper:

```sh
tools/check-reference --report firmware/build/validation/reference-clock.json --timeout 60
```

This temporarily selects the existing 10 MHz clock instead of PPS, measures
lock, captures 25,000 receive samples, selects no reference to check loss,
restores the reference and checks reacquisition. Its EXIT trap restores the
previous PPS selection. It sends no transmit samples and leaves Output 2
unchanged. Close radio clients and finish any recording first. The FPGA source
corrects `CLKIN_10MHz_REQ` to an output so its clock buffer can be enabled;
this hardware change requires the rebuilt FPGA image. A selected clock setting
alone does not establish that the physical reference is locked.

The Receive scanning and verification panel exposes scan, throughput and
endurance jobs with the same exclusive radio owner and Stop control. CLI equivalents:

```sh
tools/run scan --profile e200-rx --frequencies 902050000 902250000 902450000 \
    --dwell .025 --cycles 3 --report .antsdr/scan.json
tools/run throughput --profile e200-rx --all-channels --duration 5 \
    --rates 1000000 5000000 10000000 15000000 20000000 25000000 \
    --report .antsdr/throughput.json
tools/run soak --profile e200-rx --duration 3600 --report .antsdr/endurance.json
```

RX fast-lock supports eight on-chip profiles within a 100 MHz calibration
window, one selected channel and zero LO offset. Each slot's 16 RAM words are
read back after programming. Recall verifies RF band, gain table, clock/filter
compatibility and LO lock; ordinary tuning exits fast-lock mode. Host/SPI
latency and analog settling are reported separately from sample timestamps;
this is not an FPGA-timed hop scheduler. It does not implement TX fast-lock.
See [AD9361 UG-570](https://www.analog.com/media/en/technical-documentation/user-guides/AD9361_Reference_Manual_UG-570.pdf)
and the [ADI no-OS driver](https://github.com/analogdevicesinc/no-OS/blob/main/drivers/rf-transceiver/ad9361/ad9361.c)
for profile format and the ALC/exit workarounds.

Throughput reports every requested rate/channel case, actual coerced rates,
errors and highest tested passing rates. Completion of a sweep does not mean
every requested rate passed. `sc16` consumes four bytes per complex sample per
channel before protocol overhead, so the 1 Gb/s link limits sustained rates
below the chip's maximum. Endurance stores stream counters, temperature,
clipping, timing and free-storage observations every ten seconds plus its final
report. Raw samples are optional via `soak --output`; available disk space is
checked before recording. Closing, cancellation and receive failures cannot
report a complete endurance test. The default records no raw samples.

Controlled RF measurements are offline analyses of existing complete captures:

```sh
tools/run analyze --input .antsdr/captures/bench.cf32 --tone-hz 62500 \
    --report .antsdr/bench.json
tools/run analyze --input .antsdr/captures/agc-step.cf32 --step-at .5 \
    --report .antsdr/agc-step.json
tools/run analyze --input .antsdr/captures/level.cf32 --reference-dbm -50 \
    --reference 'Measured -50 dBm at RX connector after attenuation' --report .antsdr/level.json
tools/run filter-response --passband .antsdr/captures/pass.cf32 \
    --stopband .antsdr/captures/stop.cf32 \
    --reference 'Equal measured input power, 62.5 kHz and 2 MHz offsets' \
    --report .antsdr/filter-response.json
```

Reports include RMS/peak/clipping, DC level, image rejection around the stated
baseband tone, and bounded-resolution RMS traces. For AGC/overload recovery,
supply the known input-step time; settling requires the remaining trace to stay
within 1 dB of its final median, with at least 20 ms after settling. RF overload
flags remain instantaneous sensor snapshots. Absolute-level correction requires
the source level and reference-plane description supplied by the operator.
Filter comparison requires identical device/channel/rate/gain/bandwidth and
manual gain, with equal-power stimuli; rejection can be noise-floor limited.
These tools do not generate RF, replace a calibrated source/power meter, or
establish bench calibration from an antenna/noise recording. RF coherence is
outside this change's scope at the owner's request.

For long tests during development, copy the UHD library, Python binding and
tools to an isolated runtime before starting. Rebuilding the shared library in
place can invalidate memory mappings in an already-running client.

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

With an E200 attached and no active radio capture, run the manual restart
regression from the repository root:

```sh
docker run --rm --network host \
    --mount "type=bind,source=$PWD,target=/work" antsdr-host:22.04 \
    python3 host/tests/ant_rx_restart.py --iterations 6 \
    --report firmware/build/validation/rx-restart.json
```

This deliberately leaves receive data unread, then checks that the next session
starts at the requested device time without metadata errors or stale samples.
It exercises both receive-only and duplex streamer setup, but sends no TX
samples. Choose a new report path for each run. The E200 driver drains the old
flow-control backlog before resetting its sequence counter and flushing host
buffers; real packet sequence errors still fail the test. After rebuilding the
host driver, restart applications such as the dashboard to load it. An SD-card
firmware update is not required for this driver fix.

GitHub Actions runs hardware-free firmware tests and the host build/tool tests.
The manually dispatched firmware workflow requires a trusted self-hosted runner
labelled `antsdr-xilinx-2019.1` with the licensed tools already installed. Neither
workflow automatically deploys to a connected radio.
