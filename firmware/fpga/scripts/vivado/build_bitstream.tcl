if {$argc < 1 || $argc > 2} {
    puts stderr "usage: build_bitstream.tcl <project.xpr> ?jobs?"
    exit 2
}
set project_file [file normalize [lindex $argv 0]]
set jobs [expr {$argc == 2 ? [lindex $argv 1] : 6}]
if {![file exists $project_file]} { error "missing Vivado project: $project_file" }

open_project $project_file
# Reset top-level synthesis as well as implementation. Reusing a completed
# synth_1 checkpoint can otherwise package stale RTL after source edits.
reset_run synth_1
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
set status [get_property STATUS [get_runs impl_1]]
if {![string match "*Complete*" $status]} {
    error "implementation did not complete successfully: $status"
}
open_run impl_1
foreach delay_type {max min} {
    set paths [get_timing_paths -quiet -delay_type $delay_type -max_paths 1]
    if {[llength $paths] == 0} { error "no $delay_type timing path available" }
    set slack [get_property SLACK [lindex $paths 0]]
    puts "Release timing $delay_type slack: $slack ns"
    if {$slack < 0} { error "release timing failed: $delay_type slack $slack ns" }
}
puts "Bitstream implementation complete: $status"
close_project
