// SPDX-License-Identifier: LGPL-3.0-or-later
`timescale 1ns/1ps
module rx_trigger_tb;
   reg clk=0; always #5 clk=~clk;
   reg reset=1, set_stb=0, external_input=0, strobe=0;
   reg [7:0] set_addr=0;
   reg [31:0] set_data=0, sample=0;
   reg [63:0] vita_time=0;
   always @(posedge clk) vita_time <= vita_time+1;
   wire [1:0] mode;
   wire enabled, fire, delayed_strobe, run, eob, dsp_run, armed, err_valid;
   wire [31:0] delayed_sample;
   wire [63:0] delayed_time;
   wire [3:0] state;
   rx_trigger gate(.clk(clk),.reset(reset),.set_stb(set_stb),.set_addr(set_addr),
      .set_data(set_data),.external_input(external_input),.armed(armed),
      .sample(sample),.strobe(strobe),.vita_time(vita_time),.mode(mode),
      .enabled(enabled),.fire(fire),.delayed_strobe(delayed_strobe),
      .delayed_sample(delayed_sample),.delayed_time(delayed_time));
   new_rx_control #(.BASE(96)) ctrl(.clk(clk),.reset(reset),.clear(1'b0),
      .set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),.vita_time(vita_time),
      .run(run),.eob(eob),.dsp_run(dsp_run),.armed(armed),
      .trigger_enabled(enabled),.trigger_event(fire),.strobe(delayed_strobe),
      .full(1'b0),.seqnum(12'd0),.sid(32'd1),.err_tdata(),.err_tlast(),
      .err_tvalid(err_valid),.err_tready(1'b1),.ibs_state(state),.debug());
   integer samples=0, ends=0;
   reg [31:0] first_data;
   reg [63:0] first_time;
   always @(posedge clk) begin
      if (reset) begin samples=0; ends=0; end
      else begin
         if (err_valid) $fatal(1,"Unexpected RX error, state %d",state);
         if (armed && (run || !dsp_run)) $fatal(1,"Armed state must run DSP only");
         if (run && delayed_strobe) begin
            if (samples==0) begin first_data=delayed_sample; first_time=delayed_time; end
            samples=samples+1;
            if(eob) ends=ends+1;
         end
      end
   end
   task write_reg(input [7:0] addr,input [31:0] data);
      begin @(negedge clk); set_addr=addr; set_data=data; set_stb=1;
            @(negedge clk); set_stb=0; repeat(2) @(negedge clk); end
   endtask
   task reset_case;
      begin reset=1; strobe=0; external_input=0; sample=0;
            repeat(8) @(negedge clk); reset=0; repeat(4) @(negedge clk); end
   endtask
   task command(input [31:0] data,input [63:0] stamp);
      begin write_reg(96,data); write_reg(97,stamp[63:32]); write_reg(98,stamp[31:0]);
            repeat(8) @(negedge clk); end
   endtask
   task sample_word(input [31:0] data);
      begin sample=data; strobe=1; @(negedge clk); strobe=0; repeat(4) @(negedge clk); end
   endtask
   reg [63:0] crossing_time, target;
   integer i;
   initial begin
      reset_case(); write_reg(104,1); write_reg(105,1000); command(32'h80000003,0);
      if(!armed) $fatal(1,"Level trigger did not arm");
      for(i=0;i<10;i=i+1) sample_word(32'h03e70000); // One count below threshold.
      if(samples!=0) $fatal(1,"Samples leaked before trigger");
      crossing_time=vita_time;
      sample_word(32'hfc180001); // -1000, exact negative threshold.
      sample_word(32'h00010002); sample_word(32'h00030004);
      if(samples!=3 || ends!=1 || first_data!=32'hfc180001 || first_time!=crossing_time)
         $fatal(1,"Triggering sample/time or finite length was lost");
      // Signed minimum must have magnitude 32768, not wrap below threshold.
      reset_case(); write_reg(104,1); write_reg(105,32768); command(32'h80000001,0);
      sample_word(32'h00008000);
      if(samples!=1 || ends!=1) $fatal(1,"Signed minimum or single-sample burst failed");
      // A prior high external input is not a new edge after arming.
      reset_case(); write_reg(104,2); external_input=1; repeat(8) @(negedge clk);
      command(32'h80000002,0); sample_word(32'h11112222);
      if(samples!=0) $fatal(1,"Stale edge fired a new capture");
      external_input=0; repeat(8) @(negedge clk);
      external_input=1; repeat(8) @(negedge clk); // Pulse between sample strobes.
      external_input=0; repeat(8) @(negedge clk);
      crossing_time=vita_time; sample_word(32'h12345678); sample_word(32'h12345679);
      if(samples!=2 || ends!=1 || first_data!=32'h12345678 || first_time!=crossing_time)
         $fatal(1,"External pulse was not retained until next sample");
      // Stop and HALT must release an arm with no trigger source.
      reset_case(); write_reg(104,2); command(32'h80000005,0); command(32'h90000000,0);
      if(armed || run || dsp_run || samples) $fatal(1,"Stop did not cancel armed capture");
      reset_case(); write_reg(104,2); command(32'h80000005,0); write_reg(99,1);
      repeat(8) @(negedge clk);
      if(armed || run || dsp_run || samples) $fatal(1,"HALT did not cancel armed capture");
      // Existing device-time command must not enable the DSP before its time.
      reset_case(); target=vita_time+150; command(32'h00000002,target);
      if(run || dsp_run) $fatal(1,"Timed capture ran early");
      wait(run); @(negedge clk);
      if(vita_time<target) $fatal(1,"Timed command started before device time");
      sample_word(32'h11111111); sample_word(32'h22222222);
      if(samples!=2 || ends!=1) $fatal(1,"Device-timed finite capture failed");
      $display("PASS: level, device time, external edge, timestamps, finite lengths and cancellation");
      $finish;
   end
   initial begin #200000; $fatal(1,"Trigger test timed out"); end
endmodule
