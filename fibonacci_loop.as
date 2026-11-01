// Fibonacci: shows the first 9 numbers (1 1 2 3 5 8 13 21 34) on the
// number display, one after another, then halts.  Uses BatPU-2's I/O ports.
//
// r1 = current number, r2 = the next one, r3 = scratch, r4 = how many left to show
// r14 / r15 hold port addresses (no offsets, so each port address lives in a register)

LDI r14, clear_number
STR r14, r0            // clear the number display
LDI r14, unsigned_mode
STR r14, r0            // show values as 0-255, not signed
LDI r15, show_number

LDI r1, 1              // F1
LDI r2, 1              // F2
LDI r4, 9              // show 9 numbers

.loop
STR r15, r1            // put the current number on the display
ADD r1, r2, r3         // next = current + following
MOV r2, r1             // current = following
MOV r3, r2             // following = next
DEC r4                 // one number done (sets the zero flag)
BRH nz, .loop          // not zero yet -> show the next one
HLT
