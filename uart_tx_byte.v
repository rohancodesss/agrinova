// Single-byte 8N1 UART transmitter.
// `tick` must pulse for exactly one hw_clk cycle at the target baud rate.
module uart_tx_byte (
    input  wire       clk,
    input  wire       tick,
    input  wire [7:0] data,
    input  wire       start,
    output reg        tx,
    output reg        busy
);

    localparam IDLE      = 2'd0;
    localparam START_BIT = 2'd1;
    localparam DATA_BITS = 2'd2;
    localparam STOP_BIT  = 2'd3;

    reg [1:0] state   = IDLE;
    reg [2:0] bit_idx  = 0;
    reg [7:0] shift    = 0;

    initial tx = 1'b1;

    always @(posedge clk) begin
        case (state)
            IDLE: begin
                tx   <= 1'b1;
                busy <= 1'b0;
                if (start) begin
                    shift <= data;
                    busy  <= 1'b1;
                    state <= START_BIT;
                end
            end

            START_BIT: if (tick) begin
                tx      <= 1'b0;
                state   <= DATA_BITS;
                bit_idx <= 0;
            end

            DATA_BITS: if (tick) begin
                tx    <= shift[0];
                shift <= shift >> 1;
                if (bit_idx == 3'd7) begin
                    state <= STOP_BIT;
                end else begin
                    bit_idx <= bit_idx + 1'b1;
                end
            end

            STOP_BIT: if (tick) begin
                tx    <= 1'b1;
                busy  <= 1'b0;
                state <= IDLE;
            end

            default: state <= IDLE;
        endcase
    end

endmodule
