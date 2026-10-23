# Modern E200 runtime

This profile builds Linux 6.12.114, Buildroot 2025.02.19, GCC 13.4.0 with the
Cortex-A9 hard-float ABI, and U-Boot 2026.07. Source URLs and SHA-256 hashes are
pinned in `sources.lock.json`; downloads and extracted sources live under the
ignored `firmware/build/modern/` directory. The original vendor build remains
available for recovery and E316 builds.

The FPGA and FSBL still use Vivado/SDK 2019.1. Modernization of the CPU runtime
does not replace the licensed FPGA toolchain. The kernel port retains the E200
NIC's DMA, flow-control and PHY changes, adapts the 6.12 interfaces, and omits the
legacy `/dev/dummy_char` interface. Runtime 2 uses a narrow NIC sysfs API;
its daemons do not open `/dev/mem` and run as separate unprivileged numeric UIDs.
The device tree reserves the FPGA TX queue's 64 MiB DDR window at `0x18000000`.
See [runtime management](../runtime/README.md) for the compatibility ABI,
network handling, discovery identity, diagnostics, and ownership protocol.

Build from the repository root:

```sh
docker build -t antsdr-modern:24.04 -f firmware/docker/Dockerfile.modern firmware/docker
docker run --rm --cpus=8 --mount "type=bind,source=$PWD,target=/work" \
    antsdr-modern:24.04 bash firmware/modern/build.sh 8
```

Build the E200 FPGA through the existing Vivado flow, then package using the
Xilinx container. Both commands use the installation's original absolute path:

```sh
export XILINX_ROOT=/home/luke/Xilinx  # adapt to your installation
docker run --rm --init --cpus=8 \
    --mount "type=bind,source=$PWD,target=/work" \
    --mount "type=bind,source=$XILINX_ROOT,target=$XILINX_ROOT,readonly" \
    --mount type=volume,source=antsdr-build-home,target=/home/builder \
    --env "VIVADO_SETTINGS_FILE=$XILINX_ROOT/Vivado/2019.1/settings64.sh" \
    --env JOBS=8 antsdr-build:2019.1 make -C fpga/antsdr_e200 rebuild
docker run --rm --init --cpus=4 \
    --mount "type=bind,source=$PWD,target=/work" \
    --mount "type=bind,source=$XILINX_ROOT,target=$XILINX_ROOT,readonly" \
    --mount type=volume,source=antsdr-build-home,target=/home/builder \
    --env "VIVADO_SETTINGS_FILE=$XILINX_ROOT/Vivado/2019.1/settings64.sh" \
    antsdr-build:2019.1 bash modern/package.sh
tools/run verify firmware/build/modern/sdimg
```

Outputs in `firmware/build/modern/sdimg/` are SD boot files, not a raw card image.
Slot deployment tests the modern kernel, rootfs and FPGA using the installed
bootloader. It does **not** replace the root `BOOT.bin`; the packaged modern
U-Boot requires a separate bootloader installation and validation. Keep a
working SD card and USB console available when changing bootloaders.

Use `tools/run deploy firmware/build/modern/sdimg --reboot` for a recoverable
runtime trial on a previously provisioned E200. The dispatcher retains the
original root image; an unconfirmed trial is rejected at the next boot. Private
SD state and the existing SSH identity are reused.

The modern profile supports the E200 only. This is a kernel and userspace port,
not a guarantee that every optional peripheral or external clock is validated.
See `../FEATURES.md` for measured results and remaining hardware checks.
