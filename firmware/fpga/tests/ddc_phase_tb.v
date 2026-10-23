`timescale 1ns/1ps
// Exercise real halfband netlists after asymmetric receive history.
module ddc_phase_tb;
  reg clk=0, rst=1, run0=0, run1=0, set_stb=0;
  reg [7:0] set_addr=0;
  reg [31:0] set_data=0;
  wire stb0, stb1;
  always #5 clk=~clk;
  ddc_chain #(.NEW_HB_DECIM(1),.DEVICE("7SERIES")) ch0
    (.clk(clk),.rst(rst),.clr(1'b0),.set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),
     .rx_fe_i(24'd0),.rx_fe_q(24'd0),.sample(),.run(run0),.strobe(stb0),.debug());
  ddc_chain #(.NEW_HB_DECIM(1),.DEVICE("7SERIES")) ch1
    (.clk(clk),.rst(rst),.clr(1'b0),.set_stb(set_stb),.set_addr(set_addr),.set_data(set_data),
     .rx_fe_i(24'd0),.rx_fe_q(24'd0),.sample(),.run(run1),.strobe(stb1),.debug());
  integer history, cycles, samples;
  initial begin
    repeat(30) @(negedge clk);
    rst=0;
    // Two halfbands and CIC /4: total decimation /16.
    set_addr=2; set_data=32'h304; set_stb=1;
    @(negedge clk); set_stb=0;
    repeat(50) @(negedge clk);
    for(history=0; history<16; history=history+1) begin
      // Only channel 0 has streamed. Vary its ending decimator phase.
      run0=1;
      repeat(1201+history) @(negedge clk);
      run0=0;
      repeat(100) @(negedge clk);
      run0=1; run1=1; samples=0;
      for(cycles=0; cycles<3000; cycles=cycles+1) begin
        @(negedge clk);
        if(stb0 !== stb1 || stb0 === 1'bx || stb1 === 1'bx) begin
          $display("FAIL: history=%0d cycle=%0d strobe0=%b strobe1=%b",history,cycles,stb0,stb1);
          $fatal(1,"RX decimation phases differ after synchronized start");
        end
        if(stb0) samples=samples+1;
      end
      if(samples<100) $fatal(1,"Too few valid samples after synchronized start");
      run0=0; run1=0;
      repeat(100) @(negedge clk);
    end
    $display("PASS: 16 asymmetric histories produce aligned dual-channel strobes");
    $finish;
  end
  initial begin #2000000; $fatal(1,"Simulation deadline exceeded"); end
endmodule
