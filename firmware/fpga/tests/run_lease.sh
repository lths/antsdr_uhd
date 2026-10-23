#!/bin/bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
lib=$(cd "$here/../lib" && pwd)
source "${XILINX_ROOT:-/opt/Xilinx}/Vivado/${XILINX_VERSION:-2019.1}/settings64.sh"
work=$(mktemp -d "${TMPDIR:-/tmp}/antsdr-lease.XXXXXX")
trap 'rm -rf "$work"' EXIT
cd "$work"
xvlog -i "$lib/control" "$lib/rfnoc/xport/eth_interface.v" \
  "$lib/control/eth_radio_stream_control.v" "$lib/rfnoc/xport/eth_ipv4_chdr64_dispatch.v" \
  "$lib/fifo/axi_fifo.v" "$lib/fifo/axi_fifo_flop2.v" "$lib/fifo/axi_fifo_bram.v" \
  "$lib/fifo/axi_packet_gate.v" "$lib/control/ram_2port.v" \
  "$here/lease_tb.v" "$here/lease_dispatch_tb.v" "$XILINX_VIVADO/data/verilog/src/glbl.v"
for top in lease_tb lease_dispatch_tb; do
  xelab --timescale 1ns/1ps -L unisims_ver "work.$top" work.glbl -s "$top-sim"
  xsim "$top-sim" --runall | tee "$top.log"
  grep -q '^PASS:' "$top.log"
done
