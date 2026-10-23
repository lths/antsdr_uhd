// SPDX-License-Identifier: LGPL-3.0-or-later
`timescale 1ns/1ps
module lease_dispatch_tb;
  reg clk=0; always #5 clk=~clk;
  reg reset=1, active=0;
  reg [63:0] data=0;
  reg valid=0, last=0;
  wire ready, uvalid, ulast, cvalid, clast;
  integer radio_packets=0, cpu_packets=0;
  eth_ipv4_chdr64_dispatch #(.REQUIRE_LEASE(1),.DROP_UNKNOWN_MAC(0)) dut (
    .clk(clk),.rst(reset),.lease_active(active),.owner_ip(32'hc0a801dc),
    .s_mac_tdata(data),.s_mac_tuser(4'd0),.s_mac_tlast(last),.s_mac_tvalid(valid),.s_mac_tready(ready),
    .m_uoe_tdata(),.m_uoe_tuser(),.m_uoe_tlast(ulast),.m_uoe_tvalid(uvalid),.m_uoe_tready(1'b1),
    .m_cpu_tdata(),.m_cpu_tuser(),.m_cpu_tlast(clast),.m_cpu_tvalid(cvalid),.m_cpu_tready(1'b1),
    .my_eth_addr(48'h02f95cf5a742),.my_ipv4_addr(32'hc0a8010a),.my_udp_chdr_port(16'd49153));
  always @(posedge clk) begin
    if (uvalid && ulast) radio_packets<=radio_packets+1;
    if (cvalid && clast) cpu_packets<=cpu_packets+1;
  end
  function [31:0] swap32(input [31:0] v); begin swap32={v[7:0],v[15:8],v[23:16],v[31:24]}; end endfunction
  function [15:0] swap16(input [15:0] v); begin swap16={v[7:0],v[15:8]}; end endfunction
  task word(input [63:0] v,input final_word); begin
    @(negedge clk); data=v; last=final_word; valid=1;
    @(posedge clk); while(!ready) @(posedge clk);
    @(negedge clk); valid=0;
  end endtask
  task packet(input [31:0] source,input [15:0] port); begin
    word(64'h0000000000000000,0); // padding + dst MAC high (DROP_UNKNOWN_MAC=0)
    word(64'h0000000042a7f55c,0);
    word({16'h4000,16'h0045,16'h0008,16'h0000},0);
    word(64'h0000114000000000,0);
    word({swap32(32'hc0a8010a),swap32(source)},0);
    word({16'h0000,16'h2c00,swap16(port),16'h1234},0);
    word(64'h0010001800000050,0); word(64'h123456789abcdef0,0); word(64'h0000000000000000,1);
    repeat(80) @(negedge clk);
  end endtask
  initial begin
    repeat(5) @(negedge clk); reset=0;
    packet(32'hc0a801dc,49200);
    if(radio_packets!=0 || cpu_packets!=0) $fatal(1,"unleased radio packet passed");
    packet(32'hc0a801dc,49100); packet(32'hc0a801dd,49101);
    if(cpu_packets!=2) $fatal(1,"CPU discovery/lease traffic blocked without owner");
    active=1;
    packet(32'hc0a801dd,49200); packet(32'hc0a801dd,49202);
    if(radio_packets!=0) $fatal(1,"nonowner radio packet passed");
    packet(32'hc0a801dc,49200); packet(32'hc0a801dc,49202);
    packet(32'hc0a801dc,49203); packet(32'hc0a801dc,49204); packet(32'hc0a801dc,49153);
    if(radio_packets!=5 || cpu_packets!=2) $fatal(1,"owner filtering wrong");
    active=0; packet(32'hc0a801dc,49202); packet(32'hc0a801dc,49100);
    if(radio_packets!=5 || cpu_packets!=3) $fatal(1,"expiry or CPU recovery failed");
    $display("PASS: radio source filtering and CPU access before and after lease expiry"); $finish;
  end
  initial begin #1000000; $fatal(1,"test timeout"); end
endmodule
