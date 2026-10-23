#!/bin/sh
set -eu
modern=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
firmware=$(dirname "$modern")
# Reuse the tested storage, identity, network and supervision scripts.
export BR2_TOOLCHAIN_EXTERNAL_PREFIX="${HOST_DIR}/bin/arm-buildroot-linux-gnueabihf"
sh "$firmware/buildroot/board/e200/post-build.sh" "$@"
# The retained BusyBox config defaults to /var/spool/cron, while current
# Buildroot installs its crontab directory under /etc/cron/crontabs.
install -d "$TARGET_DIR/etc/default"
printf '%s\n' "CROND_ARGS='-c /etc/cron/crontabs'" > "$TARGET_DIR/etc/default/crond"
python3 "$firmware/scripts/build_info.py" --board e200 --target-dir "$TARGET_DIR" \
    --compiler "${BR2_TOOLCHAIN_EXTERNAL_PREFIX}-gcc" \
    --linux-source "$firmware/build/modern/src/linux-6.12.114" \
    --u-boot-source "$firmware/build/modern/src/u-boot-2026.07" \
    --buildroot-source "$firmware/build/modern/src/buildroot-2025.02.19"
