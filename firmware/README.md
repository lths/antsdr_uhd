# ANTSDR E200/E310V2 Firmware Build

English | [简体中文](README_CN.md)

This directory builds complete boot firmware for ANTSDR E200 and E310V2,
including:

- FPGA bitstream and HDF hardware definition
- FSBL and U-Boot
- Linux kernel and device tree
- Buildroot root filesystem
- Files ready to copy to the SD card FAT partition

The current flow uses Vivado/SDK 2019.1. FPGA projects are recreated from
version-controlled Tcl, RTL, XCI, and XDC files. Generated Vivado directories
such as `.xpr`, `.runs`, and `.srcs` are not tracked by Git.

## Build environment

Ubuntu is recommended. Install the basic dependencies:

```sh
sudo apt-get update
sudo apt-get install -y \
    git build-essential fakeroot ccache bc bison flex \
    libncurses5-dev libssl-dev libtinfo5 \
    cpio zip unzip rsync file wget mtools \
    device-tree-compiler u-boot-tools
```

The default Xilinx installation paths are:

```text
/opt/Xilinx/Vivado/2019.1
/opt/Xilinx/SDK/2019.1
```

You normally do not need to set `XILINX_ROOT`. The build script uses
`/opt/Xilinx` by default. If the installation is elsewhere, either set the
common root:

```sh
export XILINX_ROOT=/path/to/Xilinx
```

or override the individual paths directly (useful when Vivado and SDK are
installed in different locations):

```sh
export VIVADO_SETTINGS=/path/to/Vivado/2019.1/settings64.sh
export SDK_SETTINGS=/path/to/SDK/2019.1/settings64.sh
export TOOLCHAIN_BIN=/path/to/SDK/2019.1/gnu/aarch32/lin/gcc-arm-linux-gnueabi/bin
```

for example:
```sh
export VIVADO_SETTINGS=/opt/Xilinx/Vivado/2019.1/settings64.sh
export SDK_SETTINGS=/opt/Xilinx/SDK/2019.1/settings64.sh
export TOOLCHAIN_BIN=/opt/Xilinx/SDK/2019.1/gnu/aarch32/lin/gcc-arm-linux-gnueabi/bin
```

## One-command SD boot image build

Run the following from the repository:

```sh
cd firmware

# E200 (default target)
scripts/build_image.sh e200

# E310V2
scripts/build_image.sh e310v2
```

The script loads the Xilinx environment, configures the ARM cross compiler,
and performs the complete build. By default, it:

1. Reuses `fpga/antsdr_<target>/artifacts/system_top.hdf` when available.
2. Falls back to an HDF in the legacy Vivado project SDK directory.
3. Builds the FPGA bitstream/HDF from the tracked project scripts if no HDF
   is available.
4. Builds U-Boot, Linux, the device tree, rootfs, FSBL, and BOOT.bin.
5. Writes the final boot files to `firmware/build_sdimg/`.

To guarantee that the image contains the latest tracked FPGA sources, force
an FPGA project recreation and rebuild:

```sh
scripts/build_image.sh e200 --rebuild-fpga
scripts/build_image.sh e310v2 --rebuild-fpga
```

The parallel job count can be specified explicitly:

```sh
scripts/build_image.sh e200 --rebuild-fpga -j 8
```

Kernel and U-Boot sub-builds inherit the outer Make job limit. The device
tree is built after the kernel to avoid races on configuration and generated
files. Each build rechecks the selected board's HDF, including when switching
between E200 and E310V2 without cleaning `build/`.

## Building with Make

If the tool environment has already been loaded manually, invoke Make
directly:

```sh
source /opt/Xilinx/SDK/2019.1/settings64.sh
source /opt/Xilinx/Vivado/2019.1/settings64.sh
export PATH=/opt/Xilinx/SDK/2019.1/gnu/aarch32/lin/gcc-arm-linux-gnueabi/bin:$PATH
export CROSS_COMPILE=arm-linux-gnueabihf-

cd firmware
make -j8 TARGET=e200 image
make -j8 TARGET=e310v2 image
```

Common targets:

| Command | Description |
|---|---|
| `make TARGET=e200 image` | Build the complete SD boot files, reusing an existing HDF |
| `make TARGET=e200 all` | Build intermediates under `build/` without assembling the SD directory |
| `make TARGET=e200 fpga-project` | Recreate only the Vivado project from Tcl |
| `make TARGET=e200 fpga-bitstream` | Recreate the project and rebuild the FPGA bitstream/HDF |
| `make TARGET=e200 fpga-export-current` | Export the currently saved GUI project state |
| `make TARGET=e200 clean-build` | Remove firmware `build/` and `build_sdimg/` |

The same targets support `TARGET=e310v2`.

## Build in an Ubuntu 18.04 container

For hosts with newer compilers and system libraries, `docker/Dockerfile`
provides the Ubuntu 18.04 dependencies used with Vivado/SDK 2019.1. Install
the Xilinx tools on the host first, including SDK and Zynq-7000 support.
The tools are mounted read-only into the container at their original path.

From the repository root:

```sh
export XILINX_ROOT=/home/luke/Xilinx
docker build -t antsdr-build:2019.1 firmware/docker
docker run --rm --init --cpus=8 \
    --mount type=bind,source="$PWD",target=/work \
    --mount type=bind,source="$XILINX_ROOT",target="$XILINX_ROOT",readonly \
    --mount type=volume,source=antsdr-build-home,target=/home/builder \
    --env XILINX_ROOT \
    antsdr-build:2019.1 bash scripts/build_image.sh e200 -j 8
```

The container runs as UID/GID 1000, which must be able to write the repository.
Build outputs remain in the host repository under `firmware/build/` and
`firmware/build_sdimg/`. The named volume holds the container user's Xilinx
settings and logs. Xilinx license and WebTalk terms still apply.

## Hardware-free regression checks

From the repository root, run:

```sh
python3 -m unittest discover -s firmware/tests -v
```

These checks use temporary build trees and stub hardware commands to exercise
parallel build ordering, job limits, HDF selection when changing boards,
updater failure status, network restarts, service lifecycle, credential
save/restore, and rootfs packaging for both boards. They require
Python 3, GNU Make, a host GCC, and standard shell tools; Xilinx tools and
device access are not required. A complete firmware build and on-device
validation still require the build environment above and the target hardware.

To also check shell and utility compatibility with a host-built BusyBox:

```sh
ANTSDR_TEST_BUSYBOX=/path/to/busybox python3 -m unittest discover -s firmware/tests -v
```

## Device maintenance

Each root filesystem contains `/etc/antsdr-release.json` with the source commit,
board, clean/dirty source state, UTC build time, component versions, and compiler.
The login banner and `/opt/VERSIONS` show the same source identity, and
`antsdr-diagnostics` includes the JSON record. Builds from source archives report
an explicit unknown revision. `SOURCE_DATE_EPOCH`, when set, supplies the build
timestamp for reproducibility. The post-build generator requires host Python 3.
SD image packaging also refreshes `build_sdimg/BUILD_INFO.json` from the rootfs
identity and writes `SHA256SUMS` for the boot files and manifest.

After deployment, compare `source_revision` on the running board against the
commit used for the build. An SD-card manifest alone does not establish which
root filesystem is currently running.

On the board, collect a read-only support report with:

```sh
antsdr-diagnostics > /tmp/antsdr-diagnostics.txt
/etc/init.d/S90device_start.sh status
```

The report includes firmware version strings, service status, Ethernet and
FPGA/DMA counters, memory usage, and recent kernel messages. Missing optional
tools do not abort the report. It does not collect credential files.

Network startup manages the Ethernet IPv4 configuration from the U-Boot
environment without duplicating stanzas on restart. Other interfaces and
Ethernet IPv6 stanzas are preserved. Device start/stop tracks the control
daemon, network helper, and discovery ping with PID files under `/run`.

`device_passwd` and `device_persistent_keys` require mounted private storage.
On E200, startup creates a 64 MiB ext4 settings image on the existing SD FAT
partition and mounts it at `/mnt/antsdr-state`. A working legacy JFFS2 store
at `/mnt/jffs2` retains priority. They serialize saves and stage snapshots under
`.antsdr/`, and publish them through atomic `.current` pointer files. Boot
restore validates checksums and also supports the older `etc/` and `root/.ssh/`
layout. Corrupt saved settings are reported as errors. If persistent storage
is unavailable, boot keeps the credentials already in the root filesystem;
save commands fail without writing into the unmounted directory.

The E200 Linux device tree does not declare QSPI flash partitions. Private SD
storage supplies persistence without changing flash or the SD partition table.
See [the operations guide](../tools/README.md) for the dashboard, receive
profiles, triggered recording, calibration, health API and recoverable updates.

## FPGA development workflow

### Recreate a project from tracked sources

```sh
cd firmware/fpga/antsdr_e200
scripts/recreate_vivado_project.sh
```

The project is generated at:

```text
vivado/project/antsdr_e200/antsdr_e200.xpr
```

This is an untracked Vivado working directory.

### Export changes from a current GUI project

Save both the project and Block Design in Vivado, then run:

```sh
cd firmware/fpga/antsdr_e200

# Export to the ignored generated/ directory without changing tracked files
scripts/export_current_project_scripts.sh

# After reviewing generated/, apply the current state to the tracked Tcl files
scripts/export_current_project_scripts.sh --apply
```

By default, the exporter prefers the legacy GUI project:

```text
antsdr_e200/antsdr_e200.xpr
```

Specify a project explicitly when needed to avoid exporting the wrong one:

```sh
scripts/export_current_project_scripts.sh \
    --project /absolute/path/to/antsdr_e200.xpr
```

The review output contains:

```text
scripts/vivado/generated/e200_ps_bd.tcl
scripts/vivado/generated/create_e200_sources.tcl
scripts/vivado/generated/PROJECT_STATE.txt
```

Running with `--apply` updates:

```text
bd/e200_ps_bd.tcl
scripts/vivado/create_e200_sources.tcl
```

Recreate the project afterward to verify that the exported state is
reproducible:

```sh
scripts/recreate_vivado_project.sh
```

E310V2 provides the same scripts under
`firmware/fpga/antsdr_e310v2/`.

See [fpga/README.md](fpga/README.md) for more details about FPGA source and
project management.

## Build artifacts

After a complete image build, `build_sdimg/` contains:

```text
BOOT.bin
antsdr.bit
uImage
uEnv.txt
devicetree.dtb
uramdisk.image.gz
```

Copy these files to the SD card FAT32 boot partition. The current rootfs is
loaded from the boot partition as `uramdisk.image.gz`; it does not need to be
extracted to a separate ext4 partition.

The main intermediate artifacts are:

```text
build/system_top.hdf
build/system_top.bit
build/fsbl.elf
build/u-boot.elf
build/uImage
build/device_tree.dtb
build/rootfs.cpio.gz
build/BOOT.bin
```

FPGA release artifacts are stored under:

```text
fpga/antsdr_e200/artifacts/
fpga/antsdr_e310v2/artifacts/
```

## Boot and network

Select the SD card as the boot source and power on the device. The default
Ethernet address is:

```text
192.168.1.10/24
```

The FPGA file in the boot partition is named `antsdr.bit`; this must match
`bitstream_image=antsdr.bit` in `uEnv.txt`.
