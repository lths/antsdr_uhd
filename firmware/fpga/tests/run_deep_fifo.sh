#!/bin/bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
source "${XILINX_ROOT:-/opt/Xilinx}/Vivado/${XILINX_VERSION:-2019.1}/settings64.sh"
work=$(mktemp -d "${TMPDIR:-/tmp}/antsdr-ddr-fifo.XXXXXX")
trap 'rm -rf "$work"' EXIT
cd "$work"
xvlog "$here/../antsdr_e200/ip/deep_fifo/src/deep_fifo_core.v" "$here/deep_fifo_tb.v"
xelab --timescale 1ns/1ps work.deep_fifo_tb -s deep_fifo_sim
xsim deep_fifo_sim --runall | tee simulation.log
grep -q '^PASS:' simulation.log
