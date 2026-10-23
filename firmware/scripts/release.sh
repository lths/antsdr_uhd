#!/bin/bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
board=${1:-e200}
case "$board" in e200|e310v2) shift ;; *) echo 'First argument must be e200 or e310v2' >&2; exit 2 ;; esac
xilinx=${XILINX_ROOT:-/home/luke/Xilinx}
docker run --rm --init --cpus="${JOBS:-8}" \
    --mount "type=bind,source=$root,target=/work" \
    --mount "type=bind,source=$xilinx,target=$xilinx,readonly" \
    --mount type=volume,source=antsdr-build-home,target=/home/builder \
    --env "XILINX_ROOT=$xilinx" antsdr-build:2019.1 \
    bash scripts/build_image.sh "$board" -j "${JOBS:-8}"
"$root/tools/run" "$@" deploy firmware/build_sdimg --reboot
