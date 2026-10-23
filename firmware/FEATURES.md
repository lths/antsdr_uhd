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
MIMO setup. Delayed capture retained the requested 50,000 pretrigger samples.
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

Hardware test artifacts and structured reports are written under
`firmware/build/validation/`. Hardware-free checks cover firmware boot and
persistence logic, control response validation, SPI exclusion/restoration,
profiles, calibration, triggers, FFT levels and dashboard access.

## FPGA assessment

The current implemented design uses 39,845/53,200 LUTs (74.90%),
44,981/106,400 registers (42.28%), 95.5/140 block RAM tiles (68.21%) and
100/220 DSPs (45.45%). Its existing routed setup slack is +0.182 ns.
These figures support keeping the first spectrum/waterfall implementation on
the host. An FPGA-resident FFT would need a concrete resource budget and a fresh
implementation/timing run; it is not enabled by this release.

See [the operations guide](../tools/README.md) for commands and recovery limits.
