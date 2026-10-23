// --------------------------------------------------------------------------------
// Copyright (c) 2019 ~ 2022 by MicroPhase Technologies Inc. 
// --------------------------------------------------------------------------------
//
// Disclaimer:
//
//  This VHDL/Verilog or C/C++ source code is intended as a design reference
//  which illustrates how these types of functions can be implemented.
//  It is the user's responsibility to verify their design for
//  consistency and functionality through the use of formal
//  verification methods.  MicroPhase provides no warranty regarding the use 
//  or functionality of this code.
//
// --------------------------------------------------------------------------------
// --------------------------------------------------------------------------------
//           
//                     MicroPhase Technologies Inc
//                     Shanghai, China
//
//                     web: http://www.microphase.cn/   
//                     email: support@microphase.cn
//
// --------------------------------------------------------------------------------
// --------------------------------------------------------------------------------
//
// Major Functions:	
//  Control the stream between ethernet and fpga radio processing module.
//  Data will be routed by udp port.
//
// --------------------------------------------------------------------------------
// --------------------------------------------------------------------------------
//
// License: LGPL-3.0-or-later
// 
// --------------------------------------------------------------------------------
// --------------------------------------------------------------------------------
//
// Revision History:
// Date          By            Revision    Change Description
//---------------------------------------------------------------------
// 2022-10-09     Chaochen Wei  1.0         Original
// 2023-05-10     Chaochen Wei  1.1         Add C2H channel buffer
// 
// --------------------------------------------------------------------------------
// --------------------------------------------------------------------------------
module eth_radio_stream_control #(
    parameter REQUIRE_LEASE = 0,
    parameter CHDR_W = 64,
    parameter USER_W = 16,
    parameter BYPASS_RX_DEEP_FIFO = 0
) (
    input   wire            clk     ,
    input   wire            rst     ,
    
    // Ethernet to Vita
    input   wire    [63:0]  e2v_tdata   ,
    input   wire    [15:0]  e2v_tuser   ,
    input   wire            e2v_tlast   ,
    input   wire            e2v_tvalid  ,
    output  wire            e2v_tready  ,

    input wire session_active,
    input wire [31:0] session_epoch,

    // control stream
    output  wire    [63:0]  ctrl_tdata  ,  
    output  wire            ctrl_tlast  ,  
    output  wire            ctrl_tvalid , 
    input   wire            ctrl_tready , 

    // h2c data stream to deep_fifo
    output  wire    [63:0]  h2c_fifo_pre_tdata      ,
    output  wire            h2c_fifo_pre_tvalid     ,
    input   wire            h2c_fifo_pre_tready     ,

    // h2c data stream from deep_fifo
    input   wire    [63:0]  h2c_fifo_post_tdata     ,
    input   wire            h2c_fifo_post_tvalid    ,
    output  wire            h2c_fifo_post_tready    ,

    input   wire    [8:0]   h2c_fifo_post_rd_count  ,
    input   wire    [8:0]   h2c_fifo_pre_wr_count   , 

    // c2h data stream to deep_fifo
    output  wire    [63:0]  c2h_fifo_pre_tdata      ,
    output  wire            c2h_fifo_pre_tvalid     ,
    input   wire            c2h_fifo_pre_tready     ,

    // c2h data stream from deep_fifo
    input   wire    [63:0]  c2h_fifo_post_tdata     ,
    input   wire            c2h_fifo_post_tvalid    ,
    output  wire            c2h_fifo_post_tready    ,

    input   wire    [8:0]   c2h_fifo_post_rd_count  ,
    input   wire    [8:0]   c2h_fifo_pre_wr_count   , 

    // data stream to radio
    output  wire    [63:0]  tx_tdata    ,  
    output  wire            tx_tlast    ,  
    output  wire            tx_tvalid   , 
    input   wire            tx_tready   ,   

    // resp stream from radio
    input   wire    [63:0]  resp_tdata  ,  
    input   wire            resp_tlast  ,  
    input   wire            resp_tvalid , 
    output  wire            resp_tready , 

    //rx data stream from radio
    input   wire    [63:0]  rx_tdata    ,  
    input   wire            rx_tlast    ,  
    input   wire            rx_tvalid   , 
    output  wire            rx_tready   , 

    // Vita to Ethernet
    output  wire    [63:0]  v2e_tdata    ,
    output  wire    [15:0]  v2e_tuser    ,
    output  wire            v2e_tlast    ,
    output  wire            v2e_tvalid   ,
    input   wire            v2e_tready   

);

    wire    [63:0]  v2d_tdata   ;
    wire    [15:0]  v2d_tuser   ;
    wire            v2d_tlast   ;
    wire            v2d_tvalid  ;
    wire            v2d_tready  ;



    wire    [63:0]  rx_buf_tdata    ;
    wire            rx_buf_tlast    ;
    wire            rx_buf_tvalid   ;
    wire            rx_buf_tready   ;
    
    chdr_trim_payload#(
        .CHDR_W        ( CHDR_W ),
        .USER_W        ( USER_W )
    )u_chdr_trim_payload(
        .clk           ( clk           ),
        .rst           ( rst | (REQUIRE_LEASE && !session_active) ),

        .s_axis_tdata  ( e2v_tdata     ),
        .s_axis_tuser  ( e2v_tuser     ),
        .s_axis_tlast  ( e2v_tlast     ),
        .s_axis_tvalid ( e2v_tvalid    ),
        .s_axis_tready ( e2v_tready    ),

        .m_axis_tdata  ( v2d_tdata     ),
        .m_axis_tuser  ( v2d_tuser     ),
        .m_axis_tlast  ( v2d_tlast     ),
        .m_axis_tvalid ( v2d_tvalid    ),
        .m_axis_tready ( v2d_tready    )
    );


    wire [63:0] split_data;
    wire split_valid, split_ready, split_last;
    stream_split#(
        .CHDR_W    ( CHDR_W ),
        .USER_W    ( USER_W ),
        .BUFFER    ( 1 )
    )u_stream_split(
        .clk       ( clk            ),
        .reset     ( rst | (REQUIRE_LEASE && !session_active) ),
        .clear     ( 1'b0           ),

        .i_tdata   ( v2d_tdata      ),
        .i_tuser   ( v2d_tuser      ),
        .i_tlast   ( v2d_tlast      ),
        .i_tvalid  ( v2d_tvalid     ),
        .i_tready  ( v2d_tready     ),

        .o0_tdata  ( ctrl_tdata     ),
        .o0_tlast  ( ctrl_tlast     ),
        .o0_tvalid ( ctrl_tvalid    ),
        .o0_tready ( ctrl_tready    ),

        .o1_tdata  ( split_data       ),
        .o1_tlast  ( split_last ),
        .o1_tvalid ( split_valid ),
        .o1_tready ( split_ready )
        
    );


    generate if (REQUIRE_LEASE) begin
      antsdr_session_queue queue_guard (
        .clk(clk), .reset(rst), .active(session_active), .epoch(session_epoch),
        .in_data(split_data), .in_last(split_last), .in_valid(split_valid), .in_ready(split_ready),
        .pre_data(h2c_fifo_pre_tdata), .pre_valid(h2c_fifo_pre_tvalid), .pre_ready(h2c_fifo_pre_tready),
        .post_data(h2c_fifo_post_tdata), .post_valid(h2c_fifo_post_tvalid), .post_ready(h2c_fifo_post_tready),
        .out_data(tx_tdata), .out_last(tx_tlast), .out_valid(tx_tvalid), .out_ready(tx_tready));
    end else begin
      assign h2c_fifo_pre_tdata = split_data;
      assign h2c_fifo_pre_tvalid = split_valid;
      assign split_ready = h2c_fifo_pre_tready;
    deep_fifo_to_radio u_deep_fifo_to_radio(
        .clk                     ( clk                     ),
        .rst                     ( rst                     ),
        .h2c_fifo_post_tdata     ( h2c_fifo_post_tdata     ),
        .h2c_fifo_post_tvalid    ( h2c_fifo_post_tvalid    ),
        .h2c_fifo_post_tready    ( h2c_fifo_post_tready    ),
        .h2c_fifo_post_rd_count  ( h2c_fifo_post_rd_count  ),
        .h2c_fifo_pre_wr_count   ( h2c_fifo_pre_wr_count   ),
        .tx_tdata                ( tx_tdata                ),
        .tx_tlast                ( tx_tlast                ),
        .tx_tvalid               ( tx_tvalid               ),
        .tx_tready               ( tx_tready               )
    );
    end endgenerate



generate
    if (BYPASS_RX_DEEP_FIFO) begin
        assign rx_buf_tdata = rx_tdata;
        assign rx_buf_tlast = rx_tlast; 
        assign rx_buf_tvalid = rx_tvalid; 
        assign rx_tready  = rx_buf_tready; 
    end  else begin
        //====================================================
        //card to host data buffer
        //====================================================
        assign c2h_fifo_pre_tdata = rx_tdata;
        assign c2h_fifo_pre_tvalid = rx_tvalid;
        assign rx_tready = c2h_fifo_pre_tready;


        deep_fifo_to_host u_deep_fifo_to_host(
            .clk                     ( clk                     ),
            .rst                     ( rst                     ),
            .c2h_fifo_post_tdata     ( c2h_fifo_post_tdata     ),
            .c2h_fifo_post_tvalid    ( c2h_fifo_post_tvalid    ),
            .c2h_fifo_post_tready    ( c2h_fifo_post_tready    ),
            .c2h_fifo_post_rd_count  (   ),
            .c2h_fifo_pre_wr_count   (   ),
            .tx_tdata                ( rx_buf_tdata            ),
            .tx_tlast                ( rx_buf_tlast            ),
            .tx_tvalid               ( rx_buf_tvalid           ),
            .tx_tready               ( rx_buf_tready           )
        );
        end
endgenerate
    

    stream_aggregation#(
        .CHDR_W    ( CHDR_W ),
        .USER_W    ( USER_W ),
        .BUFFER    ( 1 )
    )u_stream_aggregation(
        .clk       ( clk       ),
        .reset     ( rst | (REQUIRE_LEASE && !session_active) ),
        .clear     ( 1'b0     ),
        .i0_tdata  ( resp_tdata  ),
        .i0_tlast  ( resp_tlast  ),
        .i0_tvalid ( resp_tvalid ),
        .i0_tready ( resp_tready ),
        .i1_tdata  ( rx_buf_tdata  ),
        .i1_tlast  ( rx_buf_tlast  ),
        .i1_tvalid ( rx_buf_tvalid ),
        .i1_tready ( rx_buf_tready ),
        .o_tdata   ( v2e_tdata   ),
        .o_tuser   ( v2e_tuser   ),
        .o_tlast   ( v2e_tlast   ),
        .o_tvalid  ( v2e_tvalid  ),
        .o_tready  ( v2e_tready  )
    );




    
endmodule
// Store one complete TX packet before entering the DDR queue. A private prefix
// carries its session epoch and exact length. Queue state is never reset on
// lease expiry: old packets drain, even across stalls and partial writes, while
// their samples are suppressed. No DDR/AXI reset is required for a handover.
module antsdr_session_queue (
  input clk, reset, active,
  input [31:0] epoch,
  input [63:0] in_data,
  input in_last, in_valid,
  output in_ready,
  output [63:0] pre_data,
  output pre_valid,
  input pre_ready,
  input [63:0] post_data,
  input post_valid,
  output post_ready,
  output [63:0] out_data,
  output out_last, out_valid,
  input out_ready
);
  reg [63:0] packet [0:255];
  reg [8:0] received, length, sent;
  reg [31:0] packet_epoch;
  reg [1:0] writer;
  localparam COLLECT=0, PREFIX=1, BODY=2, DISCARD=3;
  assign in_ready = writer == COLLECT || writer == DISCARD;
  assign pre_valid = writer == PREFIX || writer == BODY;
  assign pre_data = writer == PREFIX ? {16'h4c51, {7'd0,length}, packet_epoch} : packet[sent[7:0]];
  wire [2:0] packet_type = {in_data[63:62],in_data[60]};
  wire header_valid = (in_data[31:0] == 32'h50 || in_data[31:0] == 32'h60) &&
      (packet_type == 0 || packet_type == 1 || packet_type == 2);
  always @(posedge clk) begin
    if (reset) begin writer<=COLLECT; received<=0; length<=0; sent<=0; packet_epoch<=0; end
    else begin
      case (writer)
        COLLECT: begin
          if (!active) received<=0;
          else if (in_valid && in_ready) begin
            packet[received[7:0]]<=in_data;
            if (received == 0) packet_epoch<=epoch;
            if (received == 0 && !header_valid) begin
              received<=0; writer<=in_last?COLLECT:DISCARD;
            end else if (in_last) begin length<=received+1; received<=0; writer<=PREFIX; end
            else if (received == 255) begin received<=0; writer<=DISCARD; end
            else received<=received+1;
          end
        end
        DISCARD: if (!active || (in_valid && in_last)) writer<=COLLECT;
        PREFIX: if (pre_ready) begin sent<=0; writer<=BODY; end
        BODY: if (pre_ready) begin
          if (sent == length-1) begin writer<=COLLECT; received<=0; end
          else sent<=sent+1;
        end
      endcase
    end
  end
  reg [15:0] remaining;
  reg [31:0] queued_epoch;
  reg queue_fault;
  wire permitted = active && queued_epoch == epoch && !queue_fault;
  assign post_ready = remaining == 0 || !permitted || out_ready;
  assign out_data = post_data;
  assign out_valid = post_valid && remaining != 0 && permitted;
  assign out_last = remaining == 1;
  always @(posedge clk) begin
    if (reset) begin remaining<=0; queued_epoch<=0; queue_fault<=0; end
    else if (post_valid && post_ready) begin
      if (remaining == 0) begin
        // Internal queue corruption fails closed until a global reset.
        if (post_data[63:48] != 16'h4c51 || post_data[47:32] == 0 || post_data[47:32] > 256)
          queue_fault<=1;
        remaining<=post_data[47:32]; queued_epoch<=post_data[31:0];
      end else remaining<=remaining-1;
    end
  end
endmodule
