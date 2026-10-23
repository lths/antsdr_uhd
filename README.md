# ANTSDR UHD

UHD host driver, FPGA sources, Linux firmware, and receive tools for MicroPhase
ANTSDR devices. This fork adds an E200 build → deploy → measure workflow,
recoverable firmware updates, persistent settings, and a local receive dashboard.

The UHD, FPGA, and Linux sources originate from [Ettus Research](https://github.com/EttusResearch)
and [MicroPhase](https://github.com/MicroPhase/antsdr_uhd). Hardware validation in
this fork uses an **E200**. E316 uses the internal build target `e310v2`; its
scripts have regression coverage, but these changes have not been tested on an
E316 device.

## Features

| Area | What is included |
| --- | --- |
| Build | Parallel kernel/device-tree ordering, board-aware HDF selection, job limits, and firmware source/build metadata |
| Device identity | Preserve a valid factory MAC; otherwise persist a card-derived local MAC |
| Settings | Private SD-backed configuration, receive profiles, SSH keys, and credential snapshots |
| Health | JSON status, network counters, bounded daemon recovery, maintenance mode, and watchdog lifecycle |
| Updates | Checksummed inactive slots, trial boot confirmation, previous-slot rollback, and original boot-file fallback |
| Control driver | Bounded response parsing and timeouts, SPI transaction locking, and E200 receive flow control |
| Receive tools | Named profiles, single/dual-channel I/Q capture, immediate/level/delayed triggers, and bounded pretrigger recording |
| Display | Local browser dashboard with health, spectrum, and waterfall |
| Calibration | Versioned, device-bound measurement records and frequency/relative-level corrections |
| Validation | Hardware-free CI, structured receive benchmarks, and optional licensed firmware build runner |

Spectrum processing runs on the host. Delayed and level triggers operate in the
host receive loop. Calibration records require a measured reference; the display
uses dBFS, and these tools do not establish absolute RF power or channel phase.
The supplied ARM radio-control daemon remains a vendor binary.

## Connect an E200

Connect its gigabit Ethernet port and USB serial console to the host. The default
board address is `192.168.1.10`, and the console is **115200 baud, 8N1**. Configure
a reachable host address/route on the connected Ethernet interface; a direct
connection works without a router. Check the negotiated link is **1 Gb/s**.

For a direct connection when the host already uses `192.168.1.x` on another
interface, use a route for `192.168.1.10/32` on the E200 interface so traffic
reaches the correct cable. Interface names and host addresses depend on the
computer.

## Build the host tools and open the dashboard

Run from the repository root with Docker available:

```sh
docker build -t antsdr-host:22.04 host/docker
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD:/work" antsdr-host:22.04 bash host/docker/build.sh
tools/run discover

read -rsp 'E200 password: ' ANTSDR_PASSWORD; echo
export ANTSDR_PASSWORD
tools/run --serial /dev/ttyUSB0 --password-env ANTSDR_PASSWORD trust-console
tools/run --password-env ANTSDR_PASSWORD health
tools/run --password-env ANTSDR_PASSWORD serve
```

Open **http://127.0.0.1:8088**. The dashboard runs on the computer and permits one
receive job at a time. The console establishes SSH host-key trust. Change
`/dev/ttyUSB0` if necessary; the serial device must be accessible to the container.
`tools/run` handles the default port's device group. The host container defaults
to UID 1000, so other users may need to adapt its runtime user and device mapping.

Profiles, calibration records, captures, and verified public host keys are stored
in `.antsdr/`. Build outputs and runtime state are excluded from Git. Passwords
are supplied through the environment; SSH identity files can be used after key
provisioning. See the [operations guide](tools/README.md) for authentication,
profile formats, trigger options, and data layout.

## Build E200 firmware

The firmware flow uses **Vivado/SDK 2019.1**, Zynq-7000 support, and the SDK's
`arm-linux-gnueabihf` compiler. Install and license those tools separately; they
are not distributed in the build container. This tree currently retains Linux
**5.2.28**, U-Boot **2016.07**, and Buildroot **2020.02.8**.

```sh
docker build -t antsdr-build:2019.1 firmware/docker
export XILINX_ROOT=/home/luke/Xilinx  # change to your installation root
docker run --rm --init --cpus=8 \
    --mount "type=bind,source=$PWD,target=/work" \
    --mount "type=bind,source=$XILINX_ROOT,target=$XILINX_ROOT,readonly" \
    --mount type=volume,source=antsdr-build-home,target=/home/builder \
    --env XILINX_ROOT antsdr-build:2019.1 \
    bash scripts/build_image.sh e200 -j 8
```

Boot files are generated in `firmware/build_sdimg/`, with `BUILD_INFO.json` and
`SHA256SUMS`. An existing E200 FPGA export is reused; add `--rebuild-fpga` to
rebuild the FPGA from the tracked sources. Select `e310v2` for an E316 build.

An SD boot-files ZIP is **not a raw disk image**. For a newly prepared FAT32 boot
partition, copy the complete generated boot-file set onto it. For an already
running device with `/etc/antsdr-release.json`, use verified slot deployment
below to retain its fallback and settings. Stock images without release metadata
need the complete SD boot-file set for their initial migration. Do not replace or format the existing `antsdr-state.ext4` settings file.

A fresh card boots the root image and creates its own private settings store,
SSH key, and card-derived MAC. Verify the copied checksums after remounting the
card, then run `trust-console` again after the first boot. Keep the previous SD
card as a fallback. See [fresh-install results and remaining checks](firmware/FEATURES.md#fresh-sd-and-txrx-validation).

## Deploy, measure, and roll back

With Ethernet and the serial console connected:

```sh
tools/run verify firmware/build_sdimg
tools/run --serial /dev/ttyUSB0 --password-env ANTSDR_PASSWORD \
    deploy firmware/build_sdimg --reboot
tools/run --password-env ANTSDR_PASSWORD health
tools/run benchmark --duration 5 --rates 1000000 5000000 10000000 \
    --report firmware/build/validation/rx-benchmark.json
tools/run capture --duration 2 --trigger-after 1 --pretrigger-seconds 0.1 \
    --output .antsdr/captures/delayed.cf32

# Restore the previous confirmed slot, or cancel a pending trial:
tools/run --serial /dev/ttyUSB0 --password-env ANTSDR_PASSWORD rollback --reboot
```

`firmware/scripts/release.sh e200 --serial /dev/ttyUSB0 --password-env ANTSDR_PASSWORD`
combines the firmware build, deploy, reboot, and health check. Set `XILINX_ROOT`
and `JOBS` as needed; build both container images first.

Deployment validates board identity, hashes, and legacy image CRCs before
publishing a trial marker. It preserves the original FSBL/U-Boot and root boot
files. A failed trial load falls back immediately; an attempted trial that was
not confirmed is rejected on the next reset. A hang before Linux starts the
watchdog may require a physical reset. Boot confirmation requires restored
settings and writable boot/private storage. SD initialization gets one bounded
controller retry when no card or SD-backed filesystem is present.

## Validation and repository layout

```sh
python3 -m unittest discover -s firmware/tests -v
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$PWD:/work" antsdr-host:22.04 \
    python3 -m unittest discover -s tools/tests -v
```

The host build script also runs focused control, packet-format, and receive
handler C++ tests. With Vivado 2019.1 installed, run the FPGA phase regression:

```sh
XILINX_ROOT=/path/to/Xilinx bash firmware/fpga/tests/run_ddc_phase.sh
```

The [operations guide](tools/README.md#tests-and-ci) also documents a manual
E200 receive restart regression. It checks that a new capture rejects stale
data after a previous stream exhausts its flow-control credits, including
duplex streamer setup, without transmitting samples.

It checks actual halfband simulation models after asymmetric receive histories.
The licensed firmware workflow runs this simulation before building an image. [CI](https://github.com/lths/antsdr_uhd/actions/workflows/tests.yml)
runs firmware and host regression checks on pushes and pull requests. The manual
firmware workflow needs a self-hosted runner with the accepted Xilinx tools.
Hardware tests are run explicitly against the connected device; CI does not
flash it automatically.

See [feature validation and FPGA assessment](firmware/FEATURES.md) and the
[operations guide](tools/README.md). Local hardware reports, logs, capture
sidecars, and benchmarks live in `firmware/build/validation/`. They record the
release tested and are not uploaded with source commits.

- `firmware/`: FPGA design, kernel, bootloader, Buildroot, firmware scripts, and tests.
- `host/`: UHD driver, Python API, examples, and focused C++ tests.
- `tools/`: Deployment CLI, profiles, calibration, receive capture, and dashboard.
- `.github/workflows/`: Automated regression checks and manual firmware build.

## Related projects

- [ANTSDR documentation](https://antsdr-doc-en.readthedocs.io/en/latest/index.html)
- [MicroPhase upstream UHD repository](https://github.com/MicroPhase/antsdr_uhd)
- [IIO firmware](https://github.com/MicroPhase/antsdr-fw-patch)
- [openwifi](https://github.com/open-sdr/openwifi)
- [srsRAN 4G](https://github.com/srsran/srsRAN_4G)
