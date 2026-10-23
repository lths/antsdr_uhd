// SPDX-License-Identifier: LGPL-3.0-or-later
// Peak-level and synchronized external-edge triggers, after the DDC.
module rx_trigger #(parameter BASE=104) (
    input clk, input reset, input set_stb, input [7:0] set_addr,
    input [31:0] set_data, input external_input, input armed,
    input [31:0] sample, input strobe, input [63:0] vita_time,
    output [1:0] mode, output enabled, output fire,
    output delayed_strobe, output [31:0] delayed_sample,
    output [63:0] delayed_time
);
    wire [15:0] threshold;
      setting_reg #(.my_addr(BASE), .width(2)) sr_mode
        (.clk(clk), .rst(reset), .strobe(set_stb), .addr(set_addr),
         .in(set_data), .out(mode), .changed());
      setting_reg #(.my_addr(BASE+1), .width(16)) sr_threshold
        (.clk(clk), .rst(reset), .strobe(set_stb), .addr(set_addr),
         .in(set_data), .out(threshold), .changed());
      // Synchronize the external PPS input independently of the time source.
      (* ASYNC_REG = "TRUE" *) reg [1:0] edge_sync;
      reg edge_previous, edge_pending;
      reg sample_strobe;
      reg [31:0] sample_data;
      reg [63:0] sample_time;
      wire rising_edge = edge_sync[1] & ~edge_previous;
      wire [15:0] abs_i = sample[31] ? -sample[31:16] : sample[31:16];
      wire [15:0] abs_q = sample[15] ? -sample[15:0] : sample[15:0];
      assign enabled = mode != 0;
      assign fire = strobe &&
          ((mode == 1 && (abs_i >= threshold || abs_q >= threshold)) ||
           (mode == 2 && (edge_pending || rising_edge)));
      always @(posedge clk) begin
         if (reset) begin
            edge_sync <= 0; edge_previous <= 0; edge_pending <= 0;
            sample_strobe <= 0; sample_data <= 0; sample_time <= 0;
         end else begin
            edge_sync <= {edge_sync[0], external_input};
            edge_previous <= edge_sync[1];
            if (!armed || fire) edge_pending <= 0;
            else if (rising_edge) edge_pending <= 1;
            sample_strobe <= strobe;
            sample_data <= sample;
            sample_time <= vita_time;
         end
      end
      assign delayed_strobe = sample_strobe;
      assign delayed_sample = sample_data;
      assign delayed_time = sample_time;
endmodule
