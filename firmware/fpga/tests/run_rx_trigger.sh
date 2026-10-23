#!/bin/bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
lib=$(cd "$here/../lib" && pwd)
source "${XILINX_ROOT:-/opt/Xilinx}/Vivado/${XILINX_VERSION:-2019.1}/settings64.sh"
work=$(mktemp -d "${TMPDIR:-/tmp}/antsdr-rx-trigger.XXXXXX")
trap 'rm -rf "$work"' EXIT
cd "$work"
xvlog "$lib/control/setting_reg.v" "$lib/fifo/axi_fifo_short.v" \
    "$lib/timing/time_compare.v" "$lib/vita_200/new_rx_control.v" \
    "$lib/vita_200/rx_trigger.v" "$here/rx_trigger_tb.v" \
    "$XILINX_VIVADO/data/verilog/src/glbl.v"
xelab --timescale 1ns/1ps -L unisims_ver work.rx_trigger_tb work.glbl -s rx_trigger_sim
xsim rx_trigger_sim --runall | tee simulation.log
grep -q '^PASS: level, device time, external edge' simulation.log
