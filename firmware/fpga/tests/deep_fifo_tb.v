// SPDX-License-Identifier: LGPL-3.0-or-later
`timescale 1ns/1ps
module deep_fifo_tb;
  reg clk=0; always #5 clk=~clk;
  reg reset=1;
  integer cycle=0, produced=0, consumed=0, pre_r=0, post_w=0;
  localparam TOTAL=8193;
  reg [63:0] pre[0:16383], post[0:16383], ram[0:1023], pending[0:255];
  wire pre_pop, post_push;
  wire [63:0] post_data;
  wire [8:0] pre_count=produced-pre_r, post_count=post_w-consumed;
  wire awvalid, awready, wvalid, wready, wlast, bready;
  wire arvalid, arready, rvalid, rready, rlast;
  wire [31:0] awaddr, araddr;
  wire [7:0] awlen, arlen;
  wire [63:0] wdata;
  integer write_left=0, write_index=0, write_length=0, write_base=0, bdelay=0;
  integer read_left=0, read_index=0, bursts=0;
  reg bvalid=0, write_busy=0;
  assign awready=!write_busy;
  assign wready=write_left!=0 && cycle%7!=0;
  assign arready=read_left==0;
  assign rvalid=read_left!=0 && cycle%5!=0;
  assign rlast=read_left==1;
  deep_fifo_core #(.log2_ram_size_addr(13), .C_M_AXI_BURST_LEN(256),
    .fifo_pre_threshold(128), .fifo_post_threshold(128),
    .C_M_AXI_TARGET_SLAVE_BASE_ADDR(0), .C_M_AXI_DATA_WIDTH(64)) dut (
    .M_AXI_ACLK(clk), .M_AXI_ARESETN(!reset),
    .fifo_pre_rd_en(pre_pop), .fifo_pre_empty(pre_count==0),
    .fifo_pre_dout(pre[pre_r]), .fifo_pre_rd_count(pre_count),
    .fifo_post_wr_en(post_push), .fifo_post_full(post_count==511),
    .fifo_post_din(post_data), .fifo_post_wr_count(post_count),
    .M_AXI_AWADDR(awaddr), .M_AXI_AWLEN(awlen), .M_AXI_AWVALID(awvalid), .M_AXI_AWREADY(awready),
    .M_AXI_WDATA(wdata), .M_AXI_WVALID(wvalid), .M_AXI_WREADY(wready), .M_AXI_WLAST(wlast),
    .M_AXI_BID(1'b0), .M_AXI_BRESP(2'd0), .M_AXI_BUSER(1'b0), .M_AXI_BVALID(bvalid), .M_AXI_BREADY(bready),
    .M_AXI_ARADDR(araddr), .M_AXI_ARLEN(arlen), .M_AXI_ARVALID(arvalid), .M_AXI_ARREADY(arready),
    .M_AXI_RID(1'b0), .M_AXI_RDATA(ram[read_index]), .M_AXI_RRESP(2'd0), .M_AXI_RUSER(1'b0),
    .M_AXI_RVALID(rvalid), .M_AXI_RREADY(rready), .M_AXI_RLAST(rlast));
  integer i;
  always @(posedge clk) if(!reset) begin
    cycle<=cycle+1;
    // Complete 183-word packets with pauses, like the session queue producer.
    if(produced<TOTAL && pre_count<510 && cycle%376<183) begin
      pre[produced]<=64'h1234000000000000+produced; produced<=produced+1;
    end
    if(pre_pop) begin
      if(pre_count==0) $fatal(1,"DDR write popped an empty input FIFO");
      pre_r<=pre_r+1;
    end
    if(post_push) begin
      if(post_count==511) $fatal(1,"DDR read overflowed output FIFO");
      post[post_w]<=post_data; post_w<=post_w+1;
    end
    if(cycle>3000 && post_count!=0 && cycle%17>3 && cycle%997>150) begin
      if(post[consumed]!==64'h1234000000000000+consumed)
        $fatal(1,"data corruption at word %0d: %h",consumed,post[consumed]);
      consumed<=consumed+1;
    end
    if(awvalid && awready) begin
      write_busy<=1; write_left<=awlen+1; write_length<=awlen+1;
      write_base<=awaddr/8; write_index<=0; bursts<=bursts+1;
    end
    if(wvalid && wready) begin
      pending[write_index]<=wdata; write_index<=write_index+1; write_left<=write_left-1;
      if(wlast!=(write_left==1)) $fatal(1,"invalid write burst boundary");
      if(wlast) bdelay<=7;
    end
    if(bdelay>0) begin bdelay<=bdelay-1; if(bdelay==1) bvalid<=1; end
    if(bvalid && bready) begin
      for(i=0;i<write_length;i=i+1) ram[(write_base+i)%1024]<=pending[i];
      bvalid<=0; write_busy<=0;
    end
    if(arvalid && arready) begin read_left<=arlen+1; read_index<=araddr/8; end
    if(rvalid && rready) begin read_index<=(read_index+1)%1024; read_left<=read_left-1; end
    if(consumed==TOTAL) begin
      if(bursts<8) $fatal(1,"test did not exercise DDR wraparound");
      $display("PASS: DDR packet pauses, input starvation, AXI stalls, delayed commit, wrap and tail drain"); $finish;
    end
    if(cycle>200000) $fatal(1,"FIFO failed to drain its final partial burst");
  end
  initial begin repeat(5) @(negedge clk); reset=0; end
endmodule
