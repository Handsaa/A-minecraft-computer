// Bouncing ball: one pixel that bounces off all four edges of the screen.
// Uses BatPU-2's I/O ports (screen is 32x32, coordinates 0..31).
// For a 48x48 screen later, change SCREEN_MAX to 47.
//
// r1 = x, r2 = y, r3 = x speed (+1 right / -1 left), r4 = y speed (+1 down / -1 up)
// r10 = last row/column, r8 = delay counter
// r11..r15 hold port addresses (no offsets, so each port address lives in a register)
define SCREEN_MAX 31
define DELAY 20

LDI r11, clear_screen_buffer
LDI r12, pixel_x
LDI r13, pixel_y
LDI r14, draw_pixel
LDI r15, buffer_screen
LDI r10, SCREEN_MAX

LDI r1, 20             // start x
LDI r2, 5              // start y
LDI r3, 1              // moving right
LDI r4, 1              // moving down

.frame
STR r11, r0            // wipe the screen buffer
STR r12, r1            // pixel x
STR r13, r2            // pixel y
STR r14, r0            // draw the ball
STR r15, r0            // show this frame

LDI r8, DELAY          // short pause so the ball is visible
.wait
DEC r8
BRH nz, .wait

ADD r1, r3, r1         // x = x + x speed
ADD r2, r4, r2         // y = y + y speed

CMP r1, r10            // at the right edge?
BRH eq, .flip_x
CMP r1, r0             // at the left edge?
BRH eq, .flip_x
.check_y
CMP r2, r10            // at the bottom edge?
BRH eq, .flip_y
CMP r2, r0             // at the top edge?
BRH eq, .flip_y
JMP .frame

.flip_x
NEG r3                 // turn around horizontally
JMP .check_y
.flip_y
NEG r4                 // turn around vertically
JMP .frame
