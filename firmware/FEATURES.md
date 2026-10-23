# E200 development and operations features

This checklist records implementation and validation of the requested roadmap.
Hardware tests run on the directly connected E200; RF tests use receive only.

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

Implementation details, operation commands and measured results will be recorded
here as each feature is validated. FPGA-resident spectrum processing requires a
separate resource and timing assessment; the first display processes samples on
the host. The supplied ARM control daemon remains a vendor binary.

## Validation

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
absolute frequency accuracy, or antenna isolation. A subsequent receive session
reported an overflow at its first sample. Repeated starts after duplex use
remain under investigation; the successful fresh-boot burst does not qualify
that sequence.

Fresh-card testing also found that failed host SSH setup could leave open
unauthenticated connections. The host tool now closes connections when setup
fails. Six successive rejected old-key connections followed by a successful
trusted connection passed on the E200; host-key checking remains strict.

Hardware test artifacts and structured reports are written under
`firmware/build/validation/`. Hardware-free checks cover firmware boot and
persistence logic, control response validation, SPI exclusion/restoration,
profiles, calibration, triggers, FFT levels and dashboard access.

## FPGA assessment

The FPGA baseline before the receive-phase reset change used 39,845/53,200 LUTs (74.90%),
44,981/106,400 registers (42.28%), 95.5/140 block RAM tiles (68.21%) and
100/220 DSPs (45.45%). Its routed setup slack was +0.182 ns. Rebuilt timing reports are retained with
the hardware validation artifacts.
These figures support keeping the first spectrum/waterfall implementation on
the host. An FPGA-resident FFT would need a concrete resource budget and a fresh
implementation/timing run; it is not enabled by this release.

See [the operations guide](../tools/README.md) for commands and recovery limits.
