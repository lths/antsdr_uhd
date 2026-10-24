// SPDX-License-Identifier: LGPL-3.0-or-later
`timescale 1ns/1ps
module lease_tb;
  reg clk=0; always #5 clk=~clk;
  reg reset=1, set_owner=0, set_timeout=0;
  reg [31:0] write_data=0;
  wire active;
  wire [31:0] owner, ms, expired, epoch;
  antsdr_lease_watchdog #(.CLOCK_HZ(10000)) watchdog (
    .clk(clk), .reset(reset), .set_owner(set_owner), .set_timeout(set_timeout),
    .write_data(write_data), .active(active), .owner_ip(owner),
    .remaining_ms(ms), .expired_count(expired), .session_epoch(epoch));
  task write_owner(input [31:0] value); begin
    @(negedge clk); set_owner=1; write_data=value;
    @(negedge clk); set_owner=0;
  end endtask
  task timeout(input [31:0] value); begin
    @(negedge clk); set_timeout=1; write_data=value;
    @(negedge clk); set_timeout=0;
  end endtask

  reg qactive=1;
  reg [31:0] qepoch=1;
  reg [63:0] input_data=0;
  reg input_last=0, input_valid=0;
  wire input_ready;
  wire [63:0] pre_data, output_data;
  wire pre_valid, pre_ready, post_ready, output_last, output_valid;
  reg output_ready=0, allow_pre=1, allow_post=1;
  reg [63:0] memory [0:4095];
  integer wp=0, rp=0, outputs=0;
  reg [63:0] observed[0:4095];
  assign pre_ready=allow_pre;
  antsdr_session_queue queue (
    .clk(clk), .reset(reset), .active(qactive), .epoch(qepoch),
    .in_data(input_data), .in_last(input_last), .in_valid(input_valid), .in_ready(input_ready),
    .pre_data(pre_data), .pre_valid(pre_valid), .pre_ready(pre_ready),
    .post_data(memory[rp]), .post_valid(rp<wp && allow_post), .post_ready(post_ready),
    .out_data(output_data), .out_last(output_last), .out_valid(output_valid), .out_ready(output_ready));
  always @(posedge clk) if (!reset) begin
    if (pre_valid && pre_ready) begin memory[wp]<=pre_data; wp<=wp+1; end
    if (rp<wp && allow_post && post_ready) rp<=rp+1;
    if (output_valid && output_ready) begin observed[outputs]<=output_data; outputs<=outputs+1; end
  end
  task word(input [63:0] value, input last); begin
    @(negedge clk); input_data=value; input_last=last; input_valid=1;
    @(posedge clk); while (!input_ready) @(posedge clk);
    @(negedge clk); input_valid=0;
  end endtask
  task cycles(input integer n); begin repeat(n) @(negedge clk); end endtask
  integer before_count, i;
  initial begin
    cycles(5); reset=0; cycles(2);
    if (active || ms || epoch) $fatal(1,"reset must close the radio");
    write_owner(32'hc0a801dc); timeout(99999);
    if (!active || ms!=3000 || epoch!=1) $fatal(1,"timeout clamp or owner grant");
    timeout(2); cycles(10); timeout(3); cycles(20);
    if (!active) $fatal(1,"renewal did not extend ownership");
    cycles(12); if (active || expired!=1) $fatal(1,"independent expiry");
    timeout(3);
    if (active || ms) $fatal(1,"late renewal reopened an expired session");
    write_owner(32'hc0a801dd); timeout(3);
    write_owner(32'hc0a801dc);
    if (active || epoch!=3) $fatal(1,"owner change must revoke and advance epoch");
    timeout(3); timeout(0); if (active) $fatal(1,"explicit revoke");
    timeout(3); if (active || ms) $fatal(1,"renewal reopened a revoked session");

    // Normal packet passes byte-for-byte; prefixes never reach the radio.
    output_ready=1;
    word(64'h0010001800000050,0); word(64'h1122334455667788,0); word(64'h99aabbccddeeff00,1);
    cycles(20);
    if (outputs!=3 || observed[0]!=64'h0010001800000050 || observed[2]!=64'h99aabbccddeeff00)
      $fatal(1,"normal queue packet changed");
    // Expire with a DDR write stalled after the private prefix.
    allow_post=0;
    word(64'h0010001800000050,0); word(64'hbad0000000000001,0); word(64'hbad0000000000002,1);
    cycles(1); allow_pre=0; qactive=0; cycles(10);
    allow_pre=1; cycles(10); qepoch=2; qactive=1; allow_post=1;
    cycles(30); if (outputs!=3) $fatal(1,"stale DDR samples escaped into a new session");
    // Expire in the middle of an output packet, with downstream stalled.
    output_ready=0;
    word(64'h0010001800000050,0); word(64'hbad0000000000003,0); word(64'hbad0000000000004,1);
    cycles(12); qactive=0; cycles(10); qepoch=3; qactive=1; output_ready=1;
    cycles(20); if (outputs!=3) $fatal(1,"stalled old packet resumed");
    // Incomplete ingress is abandoned before a prefix is committed.
    word(64'h0010001800000050,0); qactive=0; cycles(4); qepoch=4; qactive=1;
    word(64'h0010001000000050,0); word(64'h0123456789abcdef,1);
    cycles(30);
    if (outputs!=5 || observed[4]!=64'h0123456789abcdef) $fatal(1,"queue alignment lost across handover");
    // Oversized ingress is discarded as one packet, then normal traffic resumes.
    word(64'h0010081000000050,0);
    for (i=1;i<258;i=i+1) word(i,i==257);
    word(64'h0010001000000050,0); word(64'hfedcba9876543210,1); cycles(30);
    if (outputs!=7 || observed[6]!=64'hfedcba9876543210) $fatal(1,"oversize recovery");
    $display("PASS: lease expiry, renewal, owner epochs and stalled DDR queue handover"); $finish;
  end
  initial begin #1000000; $fatal(1,"test timeout"); end
endmodule
