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

## Validation status

Implementation is complete; the combined on-device release is pending validation.
The host driver, Python package and focused control/transport C++ tests build
successfully. Firmware tests pass with both host tools and BusyBox. The new
recorder has captured approximately one million samples at 1 MS/s without
reported receive errors. The ARM health probe has also passed a RAM-only E200
check. Full slot deployment, persistence, recovery and dashboard tests follow.

See [the operations guide](../tools/README.md) for commands and recovery limits.
