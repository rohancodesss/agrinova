module top (
    input  wire hw_clk,     // 12MHz onboard oscillator (pin 20)
    output wire uarttx,     // UART TX to Mac (pin 14)
    output wire led_red,
    output wire led_green,
    output wire led_blue,
    input  wire ir_in       // IR sensor DO (pin 38, active LOW on detection)
);

    localparam CLK_FREQ       = 12_000_000;
    localparam BAUD_RATE      = 9600;
    localparam CLKS_PER_BIT   = CLK_FREQ / BAUD_RATE;  // 1250

    // ---------------------------------------------------------------
    // Baud tick generator: one-cycle pulse every 1250 hw_clk cycles
    // ---------------------------------------------------------------
    reg [10:0] baud_count = 0;
    reg        baud_tick  = 0;

    always @(posedge hw_clk) begin
        if (baud_count == CLKS_PER_BIT - 1) begin
            baud_count <= 0;
            baud_tick  <= 1'b1;
        end else begin
            baud_count <= baud_count + 1'b1;
            baud_tick  <= 1'b0;
        end
    end

    // ---------------------------------------------------------------
    // IR sensor (pin 38): DO is LOW while something is detected.
    // Sync input, then request an IR message on the clear->detect edge.
    // Cooldown of 10s stops repeat alerts while the beam stays blocked.
    // ---------------------------------------------------------------
    localparam IR_COOLDOWN = CLK_FREQ * 10;  // 10 seconds

    reg [1:0]  ir_sync = 2'b11;   // idle = HIGH (nothing detected)
    reg [27:0] ir_cooldown = 0;
    reg ir_req = 0;   // toggles once per detection

    always @(posedge hw_clk) begin
        ir_sync <= {ir_sync[0], ir_in};

        if (ir_cooldown != 0)
            ir_cooldown <= ir_cooldown - 1'b1;

        // falling edge = detection started
        if (ir_sync[1] == 1'b1 && ir_sync[0] == 1'b0 && ir_cooldown == 0) begin
            ir_req <= ~ir_req;
            ir_cooldown <= IR_COOLDOWN;
        end
    end

    // ---------------------------------------------------------------
    // Message ROM: "IR\n"
    // ---------------------------------------------------------------
    reg [7:0] message [0:2];
    initial begin
        message[0] = "I"; message[1] = "R"; message[2] = "\n";
    end

    // ---------------------------------------------------------------
    // Message sequencer
    // ---------------------------------------------------------------
    reg [1:0] msg_index = 0;
    reg       sending_msg = 0;
    reg       byte_start  = 0;
    reg       ir_ack      = 0;
    wire      byte_busy;

    always @(posedge hw_clk) begin
        byte_start <= 1'b0;

        if (!sending_msg) begin
            if (ir_req != ir_ack) begin
                ir_ack      <= ir_req;
                sending_msg <= 1'b1;
                msg_index   <= 2'd0;
                byte_start  <= 1'b1;
            end
        end else if (!byte_busy && !byte_start) begin
            if (msg_index == 2'd2) begin
                sending_msg <= 1'b0;
            end else begin
                msg_index  <= msg_index + 1'b1;
                byte_start <= 1'b1;
            end
        end
    end

    uart_tx_byte u_tx (
        .clk   (hw_clk),
        .tick  (baud_tick),
        .data  (message[msg_index]),
        .start (byte_start),
        .tx    (uarttx),
        .busy  (byte_busy)
    );

    // LEDs: green while transmitting, blue while IR sensor is detecting
    assign led_red   = 1'b0;  // disabled to prevent burnout
    assign led_green = sending_msg;
    assign led_blue  = ~ir_sync[1];

endmodule
