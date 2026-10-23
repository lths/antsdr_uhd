#!/bin/bash
# Run in antsdr-modern:24.04, with the repository mounted at /work.
set -euo pipefail
modern=$(cd "$(dirname "$0")" && pwd)
base="$modern/../build/modern"
jobs=${1:-8}
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || { echo 'Expected a positive job count' >&2; exit 2; }
python3 "$modern/scripts/fetch.py"
rootfs="$base/rootfs-hardfloat"
make -C "$base/src/buildroot-2025.02.19" O="$rootfs" BR2_EXTERNAL="$modern" e200_defconfig
make -C "$rootfs" -j"$jobs" toolchain
cross="$rootfs/host/bin/arm-buildroot-linux-gnueabihf-"
python3 "$modern/scripts/prepare_kernel.py" "$base/src/linux-6.12.114"
make -C "$base/src/linux-6.12.114" O="$base/kernel" ARCH=arm CROSS_COMPILE="$cross" \
    KCONFIG_ALLCONFIG="$modern/board/linux.config" allnoconfig
# Fail early if a dependency silently removed essential board support.
for option in MMU ARCH_ZYNQ SMP VFP NEON MMC_BLOCK BLK_DEV_LOOP EXT4_FS VFAT_FS \
    NI_XGE_MANAGEMENT_ENET REALTEK_PHY OF_FPGA_REGION DEVMEM BINFMT_SCRIPT \
    COMPAT_32BIT_TIME BLK_DEV_WRITE_MOUNTED; do
    grep -qx "CONFIG_${option}=y" "$base/kernel/.config" || { echo "Missing kernel option $option" >&2; exit 1; }
done
make -C "$base/kernel" ARCH=arm CROSS_COMPILE="$cross" -j"$jobs" \
    LOADADDR=0x8000 uImage xilinx/zynq-e200.dtb
cp "$modern/board/zynq-e200-uboot.dts" "$base/src/u-boot-2026.07/arch/arm/dts/zynq-e200.dts"
mkdir -p "$base/uboot"
cp "$modern/board/uboot.config" "$base/uboot/.config"
make -C "$base/src/u-boot-2026.07" O="$base/uboot" ARCH=arm CROSS_COMPILE="$cross" olddefconfig
make -C "$base/uboot" ARCH=arm CROSS_COMPILE="$cross" -j"$jobs"
make -C "$rootfs" -j"$jobs"
echo 'Modern runtime built. Package with modern/package.sh in the Xilinx build container.'
