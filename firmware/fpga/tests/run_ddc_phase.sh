#!/bin/bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
lib=$(cd "$here/../lib" && pwd)
source "${XILINX_ROOT:-/opt/Xilinx}/Vivado/${XILINX_VERSION:-2019.1}/settings64.sh"
work=$(mktemp -d "${TMPDIR:-/tmp}/antsdr-ddc-phase.XXXXXX")
trap 'rm -rf "$work"' EXIT
cd "$work"
xvlog "$lib/control/setting_reg.v" \
    "$lib/dsp/sign_extend.v" "$lib/dsp/cordic_stage.v" "$lib/dsp/cordic_z24.v" \
    "$lib/dsp/cic_strober.v" "$lib/dsp/cic_decim.v" "$lib/dsp/cic_dec_shifter.v" \
    "$lib/dsp/add2_and_clip.v" "$lib/dsp/add2_and_clip_reg.v" "$lib/dsp/clip.v" "$lib/dsp/clip_reg.v" "$lib/dsp/round.v" "$lib/dsp/round_sd.v" \
    "$lib/ngc/hbdec1.v" "$lib/ngc/hbdec2.v" \
    "${ANTSDR_DDC_SOURCE:-$lib/dsp/ddc_chain.v}" "$here/ddc_phase_tb.v" "$XILINX_VIVADO/data/verilog/src/glbl.v"
xelab --timescale 1ns/1ps -L unisims_ver -L unimacro_ver work.ddc_phase_tb work.glbl -s ddc_phase_sim
xsim ddc_phase_sim --runall | tee simulation.log
# XSim 2019.1 can return zero after $fatal; require the completion marker.
grep -q "^PASS: 16 asymmetric histories" simulation.log
