#!/bin/bash
set -euo pipefail
modern=$(cd "$(dirname "$0")" && pwd)
firmware=$(dirname "$modern")
base="$firmware/build/modern"
out="$base/sdimg"
settings="${VIVADO_SETTINGS_FILE:-/opt/Xilinx/Vivado/2019.1/settings64.sh}"
source "$settings"
export VIVADO_SETTINGS="$settings"
export PATH="$(dirname "$settings")/../../SDK/2019.1/gnu/aarch32/lin/gcc-arm-linux-gnueabi/bin:$PATH"
make -C "$firmware" TARGET=e200 build/fsbl.elf
mkdir -p "$out"
cat > "$base/boot.bif" <<EOF
image : { [bootloader] $firmware/build/fsbl.elf $base/uboot/u-boot.elf }
EOF
bootgen -image "$base/boot.bif" -o i "$out/BOOT.bin" -w on
cp "$firmware/fpga/antsdr_e200/artifacts/antsdr_e200.bit" "$out/antsdr.bit"
cp "$base/kernel/arch/arm/boot/uImage" "$out/uImage"
cp "$base/kernel/arch/arm/boot/dts/xilinx/zynq-e200.dtb" "$out/devicetree.dtb"
cp "$modern/board/uEnv.txt" "$out/uEnv.txt"
mkimage -A arm -T script -C none -n 'ANTSDR recoverable SD boot' -d "$firmware/scripts/boot.cmd" "$out/boot.scr"
mkimage -A arm -T ramdisk -C gzip -d "$base/rootfs-hardfloat/images/rootfs.cpio.gz" "$out/uramdisk.image.gz"
cp "$base/rootfs-hardfloat/target/etc/antsdr-release.json" "$out/BUILD_INFO.json"
(cd "$out" && sha256sum BOOT.bin boot.scr antsdr.bit uImage uEnv.txt devicetree.dtb uramdisk.image.gz BUILD_INFO.json > SHA256SUMS)
echo "Modern E200 SD boot files: $out"
