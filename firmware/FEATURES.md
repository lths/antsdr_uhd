# E200 development and operations features

This checklist records implementation and validation of the requested roadmap.
Hardware tests run on the directly connected E200. RF tests include brief,
minimum-gain 902.1 MHz bursts and simultaneous receive.

- [x] Stable MAC identity across reboot
- [x] SD-backed private configuration and persistent SSH keys
- [x] Service health, bounded recovery, restart history and watchdog lifecycle
- [x] Packaged deployment, verification and rollback command
- [x] Repeatable receive-rate benchmarks and structured results
- [x] Bounded control-packet parsing, timeouts and SPI transaction locking
- [x] Automated hardware-free CI, with optional licensed build runner
- [x] Local device dashboard and health API
- [x] Validated named receive profiles
- [x] Triggered I/Q recording with capture metadata
- [x] Spectrum and waterfall display
- [x] Versioned calibration records
- [x] Trial boots, boot confirmation and automatic release fallback
- [x] FPGA peak-level and external PPS-edge trigger modes
- [x] Device-timed finite captures and dashboard trigger controls
- [x] Finite transmit dashboard controls with burst acknowledgements
- [x] Source E200 discovery, IPv4 setup and identity runtime
- [x] Modern E200 kernel, root filesystem and packaged U-Boot build
- [x] Safe FPGA management compatibility checks before register access
- [x] Live daemon diagnostics and FPGA readback in the dashboard
- [x] Netlink handling for address removal, link loss and recovery
- [x] Restricted NIC management API and separate unprivileged daemons
- [x] Automatic EEPROM board-revision identification
- [x] Device ownership leases with independent FPGA expiry and DDR session isolation
- [x] AD9361 receiver tuning, per-channel AGC, live sensors and verified FIR tap loading (host driver/tools)
- [x] GPS/PPS dashboard health and validated PPS/NMEA UTC sample tags
- [x] Private SigMF export with UTC, channel interleaving, checksum and loss metadata
- [x] RX fast-lock profiles and bounded receive scanning
- [x] Single/dual receive-rate sweeps and receive endurance reporting
- [x] Controlled receiver step/level/image/filter analysis tools

Implementation details, operation commands and measured results are recorded
below. FPGA-resident spectrum processing requires a
separate resource and timing assessment; the first display processes samples on
the host. The E200 discovery/network runtime is now built from source; E316
retains the vendor daemon.

## Timing, scan and measurement tools

The host tools include a read-only LBE-1421 USB bridge, active-owner PPS
monitoring, conservative PPS/NMEA UTC association, SigMF recording export,
eight-slot RX fast-lock scanning, and throughput/endurance jobs in the CLI and
dashboard. Controlled receiver analyses cover RMS/peak/clipping, DC/image
levels, known-step settling and equal-input filter rejection. Absolute levels
require an operator-supplied measured reference. RF coherence is excluded at
the owner's request.

External clock profiles require confirmed lock and invalidate a capture if lock
is lost. The E200 FPGA now declares the reference-buffer enable pin
`CLKIN_10MHz_REQ` as an output; its previous input direction left the enable
undriven. The rebuilt design uses 40,631/53,200 LUTs, 95.5/140 block RAM tiles,
and 100/220 DSPs. Routed setup/hold slack is +0.232/+0.034 ns with no failing
timing endpoints. The placed I/O report confirms the output on package pin M20.
Reference-lock and extended-streaming results are recorded separately from
the implementation checks under `firmware/build/validation/`.

See the [timing and measurement guide](../tools/README.md#timing-recordings-and-measurements)
for commands, UTC association limits, shared-connector behavior and bench setup.

## Validation

The AD9361 host extension passed an 11-case receive-only hardware matrix:
independent manual gains, slow/fast and mixed-channel AGC, LO-offset tuning,
independent DC/IQ toggles, zero FIR coefficients followed by default-filter
restoration, and live telemetry at 5/10 MS/s. Captures completed without RX
metadata errors or overflows. Fast AGC reached hardware state 5 (gain locked).
Browser automation saved per-channel tuning, disabled manual gain fields during
AGC, and completed radio/filter inspection. These changes require a rebuilt
host driver/Python API; the existing E200 SD/FPGA image is compatible.
Overload-bit decoding has register-model coverage; absolute RF calibration and
controlled overload/AGC step-response measurements still require a reference
source. See the [receiver tuning guide](../tools/README.md#ad9361-receiver-tuning).

The combined E200 implementation has passed on-device checks for private SD
settings, restored radio/network configuration, stable MAC and SSH host keys,
maintenance behavior, daemon crash recovery, trial confirmation and automatic
fallback from a trial with a missing kernel. The live dashboard has displayed
real spectrum data and device health. Receive tests passed at 1, 5 and 10 MS/s;
two-channel 1 MS/s capture passed after synchronizing timekeepers after final
MIMO setup. The driver also waits for both timekeeper-arm writes to be
acknowledged before the shared sync pulse. FPGA halfband decimators reset their
phase while idle, addressing the persistent timestamp offset found when moving
from single-channel to dual-channel capture. A licensed simulation uses the
actual halfband netlists and asymmetric receive histories to check this ordering.
Delayed capture retained the requested 50,000 pretrigger samples.
Warm-reset testing also exposed an SD initialization timeout (`card never left
busy state`). Startup now waits for enumeration and retries the unmounted
controller once, only when no card is present and no SD/loop filesystem is
mounted. Health requires writable boot/private storage and completed settings
restoration; a missing boot mount is reported as an unknown confirmed slot.
Automatic SSH-key persistence waits for successful restoration. If storage
remains unavailable, confirmation stops with an error and the running system
remains available for diagnosis; a subsequent reset rejects an unconfirmed trial.

Calibration application is software-tested; RF calibration still requires a
measured reference.

### Fresh SD and TX/RX validation

On 2026-10-23, an E200 booted the complete `dfe9da66b` release from a fresh FAT32
SD card. All boot-file hashes matched after host remount and on-device readback.
Private state creation and write/read checks passed, and a reboot preserved the
new MAC and SSH key. The first card reader failed with USB resets and write
errors; filesystem repair and installation through a different reader passed.

A finite 50 ms burst at 902.1 MHz, with minimum hardware TX gain and digitally
attenuated samples, produced a 12.0 dB received tone rise and 14.6 dB fall with
no stream errors or clipping. TX and RX antennas were about 12 inches apart.
This checks basic TX/RX operation; it does not establish calibrated power,
absolute frequency accuracy, or antenna isolation.

A subsequent receive session exposed a host-driver startup ordering bug. Resetting
E200 source flow control after the host buffer flush released old packets into
the new capture, causing an initial sequence error and stale timestamps. The
driver now drains flow-controlled data before resetting the FPGA sequence
counter and flushing the new receiver. Packet loss detection remains enabled.
The failure was reproduced without transmitting; six deliberate backlog/restart
cases passed with the fix, including single/dual receive and 1, 5 and 10 MS/s
settings. Three consecutive 50 ms bursts at 902.1 MHz, each followed by a clean
receive-only capture, passed without rebooting. This fix requires rebuilding
the host driver; the installed `dfe9da66b` SD firmware was retained.

Fresh-card testing also found that failed host SSH setup could leave open
unauthenticated connections. The host tool now closes connections when setup
fails. Six successive rejected old-key connections followed by a successful
trusted connection passed on the E200; host-key checking remains strict.

Hardware test artifacts and structured reports are written under
`firmware/build/validation/`. Hardware-free checks cover firmware boot and
persistence logic, control response validation, SPI exclusion/restoration,
profiles, calibration, triggers, FFT levels and dashboard access.

## FPGA assessment

The hardware-trigger implementation uses 39,986/53,200 LUTs (75.16%),
45,199/106,400 registers (42.48%), 95.5/140 block RAM tiles and 100/220 DSPs.
Routed setup/hold slack is +0.163/+0.024 ns, with zero total negative slack.
The build now resets top-level synthesis before implementation so edited RTL
cannot be hidden by a completed synthesis checkpoint.

The FPGA baseline before the receive-phase reset change used 39,845/53,200 LUTs (74.90%),
44,981/106,400 registers (42.28%), 95.5/140 block RAM tiles (68.21%) and
100/220 DSPs (45.45%). Its routed setup slack was +0.182 ns. Rebuilt timing reports are retained with
the hardware validation artifacts.
These figures support keeping the first spectrum/waterfall implementation on
the host. An FPGA-resident FFT would need a concrete resource budget and a fresh
implementation/timing run; it is not enabled by this release.

See [the operations guide](../tools/README.md) for commands and recovery limits.

## Modern runtime and hardware controls

The modern profile boots Linux 6.12.114 and Buildroot 2025.02.19 with GCC 13.4.0.
The E200 source runtime preserves the existing DNA-derived serial and discovery
packet format. The first runtime trial exposed kernel API/configuration issues;
it automatically rejected the unconfirmed image and booted the original root.
The corrected trial mounted private SD state, restored MAC and SSH identity,
negotiated gigabit Ethernet, and confirmed its slot with healthy services.

Validation includes six deliberate RX backlog/restart cases, single/dual-channel
finite device-time captures, FPGA level capture on each channel, high-threshold
and no-external-edge timeouts, and the live dashboard capture API. A 50 ms,
minimum-gain 902.1 MHz burst was acknowledged by the FPGA; simultaneous receive
measured the tone approximately 34 dB above its baseline with no RX metadata
errors. Tone, two-tone and chirp continuity and error/cancellation paths have
host regression coverage. The licensed trigger simulation checks the triggering
sample and timestamp, signed limits, an external pulse between sample strobes,
stale-edge rejection, finite lengths, stop/HALT and device-time commands.

U-Boot 2026.07 was chainloaded from RAM and successfully booted the modern
runtime from SD. Slot deployment preserves the existing bootloader; consult the
modern build guide before separately installing its packaged `BOOT.bin`.

Run the receive-only trigger matrix in the host container:

```sh
python3 host/tests/ant_trigger_modes.py --expect-external-timeout \
    --report firmware/build/validation/trigger-matrix.json
```

Physical external PPS input was verified on the AD9361 E200 with a GPS-locked
Leo Bodnar LBE-1421 set to PPS on Output 1. The check detected ten consecutive
external pulses, synchronized device time on PPS, and completed nine finite
captures across RX0, RX1, and both channels at 1 MS/s without receive errors.
First-sample timestamps were within one sample period of the PPS latch.
Repeat with `host/tests/ant_pps_input.py`; see the operations guide for setup.
This verifies edge-triggered reception and relative time synchronization;
absolute UTC accuracy and external frequency-reference locking remain untested.
FPGA level mode supports one RX channel; device-time and external
edge modes support both. Hardware modes do not retain pretrigger samples. The
modern profile currently targets SD boot on E200; QSPI update paths, E316,
external reference clocks, RF calibration and prolonged soak tests remain
outside the hardware validation performed here.

## Runtime 2 management and ownership

Runtime 2 adds all six management improvements: safe FPGA compatibility
checking, dashboard diagnostics, Netlink network updates, restricted driver
access, EEPROM board identification, and ownership enforced by the FPGA.
The daemons run as UIDs 65301/65302 with zero effective capabilities and no
physical-memory mappings. The modern device tree reserves the FPGA's 64 MiB TX
queue from Linux allocation.

On the connected E200, IPv4 removal reached the FPGA in 20–30 ms and link-down
in 50 ms. Identity came from EEPROM and the existing DNA serial was preserved.
Physical tests confirmed competing lease requests are rejected, control packets
are gated by owner IP, ownership expires with the daemon stopped, and a real
UHD process renews through a six-second receive session. Killing that process
left the device available again in approximately 2.6 seconds.

Transmit validation exposed a vendor DDR FIFO bug: its 128-word threshold could
start a 256-word AXI burst with insufficient input. The corrected core waits
for a complete burst and for the write response before exposing memory to
readers. Its regression checks packet pauses, AXI stalls, delayed write
commitment, wraparound and final partial-burst drainage. The original core
fails that test by popping an empty input FIFO. Ownership also remains attached
to the actual control and sample transports through controller teardown.

See [runtime management](runtime/README.md) for the protocol, permissions,
compatibility requirements, and diagnostics. With an idle E200, this manual
regression checks device ownership and control gating without sending RF:

```sh
python3 host/tests/ant_device_lease.py \
    --report firmware/build/validation/device-lease.json
```

This test requires exclusive radio access but no SSH credentials. FPGA tests
`run_lease.sh` and `run_deep_fifo.sh` require the licensed Vivado installation.
External PPS and receive-trigger verification uses `host/tests/ant_pps_input.py`
with a connected 1 PPS source; see the hardware validation notes above.
