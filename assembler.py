"""Assembler for the current 32-bit Minecraft CPU instruction subset.

Current instruction layout (bit 31 is the most significant bit):
    31..27  opcode       (5 bits)
    26..22  Read1        (5 bits)
    21..17  Read2        (5 bits)
    16..12  Write        (5 bits)
    11..4   immediate    (8 bits)
    3..0    reserved     (4 bits, written as zero)

Important datapath conventions:
* Ordinary three-register ALU operations read Read1/Read2 and write Write.
* LDI uses Read1 as its destination, because the physical destination mux
  routes the immediate directly to the Read1-side register destination.
* ADI reads Read1, uses the immediate instead of Read2, and writes Write.
* RSHIFT reads Read1 and writes Write.  It accepts either one register
  (in-place) or source,destination.

Jumps use absolute 10-bit addresses (like BatPU-2).  Opcodes: 14 JMP, 15 BRH,
16 CAL, 17 RET, 18 LOD (Read1 = address register, Write = destination),
19 STR (Read1 = address register, Read2 = data register).  No offsets.

Opcode 11 is deliberately left unassigned for now. LSHIFT is an assembler
pseudo-instruction translated to ADD source,source,destination; it does not
emit opcode 11.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

INSTRUCTION_LIMIT = 1024
WORD_BITS = 32

OPCODES: Dict[str, int] = {
    "nop": 0,
    "hlt": 1,
    "add": 2,
    "sub": 3,
    "or": 4,
    "nor": 5,
    "and": 6,
    "nand": 7,
    "xor": 8,
    "xnor": 9,
    "rshift": 10,
    # 11 reserved; LSHIFT is a pseudo-instruction.
    "ldi": 12,
    "adi": 13,
    "jmp": 14,
    "brh": 15,
    "cal": 16,
    "ret": 17,
    "lod": 18,
    "str": 19,
}

REGISTER_COUNT = 32
IMMEDIATE_MIN = -128
IMMEDIATE_MAX = 255

# ---------------------------------------------------------------------------
# Jump / branch / call encoding.  Same idea as mattbatwings' BatPU-2: every
# jump target is an ABSOLUTE 10-bit instruction address (no offsets).
#
# ASSUMED WIRING - change these two numbers if your PC reads a different place:
#   address of JMP / BRH / CAL  -> instruction bits 9..0
#   condition of BRH (2 bits)   -> instruction bits 12..11 (bit 11 = low bit)
# ---------------------------------------------------------------------------
ADDRESS_BITS = 10
ADDRESS_SHIFT = 0
CONDITION_SHIFT = 11

# Same condition codes and spellings as BatPU-2.  The carry flag after a
# subtraction/compare is 1 when there was NO borrow (first value >= second).
CONDITIONS = {
    "eq": 0, "=": 0, "z": 0, "zero": 0,
    "ne": 1, "!=": 1, "nz": 1, "notzero": 1,
    "ge": 2, ">=": 2, "c": 2, "carry": 2,
    "lt": 3, "<": 3, "nc": 3, "notcarry": 3,
}

# Same memory-mapped I/O ports as BatPU-2: the last 16 bytes of data memory.
PORT_NAMES = [
    "pixel_x", "pixel_y", "draw_pixel", "clear_pixel", "load_pixel",
    "buffer_screen", "clear_screen_buffer", "write_char", "buffer_chars",
    "clear_chars_buffer", "show_number", "clear_number", "signed_mode",
    "unsigned_mode", "rng", "controller_input",
]
PORT_BASE = 240


def base_symbols() -> Dict[str, int]:
    symbols: Dict[str, int] = dict(CONDITIONS)
    for index, name in enumerate(PORT_NAMES):
        symbols[name] = PORT_BASE + index
    return symbols


@dataclass
class SourceInstruction:
    tokens: List[str]
    line_number: int


def strip_comment(line: str) -> str:
    """Remove //, ;, or # comments without disturbing quoted text."""
    quote = None
    escaped = False
    i = 0
    while i < len(line):
        char = line[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if char == "\\" and quote:
            escaped = True
            i += 1
            continue
        if quote:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in ("'", '"'):
            quote = char
            i += 1
            continue
        if line.startswith("//", i) or char in (";", "#"):
            return line[:i]
        i += 1
    return line


def tokenize(line: str, line_number: int) -> List[str]:
    """Split assembly tokens; commas are separators and quotes are preserved."""
    try:
        lexer = shlex.shlex(line.replace(",", " "), posix=False)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return [token.strip() for token in lexer if token.strip()]
    except ValueError as exc:
        raise ValueError(f"Line {line_number}: {exc}") from exc


def parse_register(token: str, line_number: int) -> int:
    match = re.fullmatch(r"[rR](\d+)", token.strip())
    if not match:
        raise ValueError(f"Line {line_number}: expected a register r0..r31, got {token!r}")
    value = int(match.group(1))
    if not 0 <= value < REGISTER_COUNT:
        raise ValueError(f"Line {line_number}: register {token!r} is outside r0..r31")
    return value


def parse_number(token: str, symbols: Dict[str, int], line_number: int) -> int:
    key = token.lower()
    if key in symbols:
        return symbols[key]
    try:
        # int(..., 0) supports decimal, 0x hex, and 0b binary.
        return int(token, 0)
    except ValueError as exc:
        raise ValueError(f"Line {line_number}: cannot resolve numeric value {token!r}") from exc


def parse_address(token: str, symbols: Dict[str, int], line_number: int) -> int:
    value = parse_number(token, symbols, line_number)
    if not 0 <= value < (1 << ADDRESS_BITS):
        raise ValueError(f"Line {line_number}: instruction address {value} does not fit in {ADDRESS_BITS} bits (0-1023)")
    return value


def parse_source(source_path: str) -> Tuple[List[SourceInstruction], Dict[str, int]]:
    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(f"Assembly file not found: {source_path}")

    instructions: List[SourceInstruction] = []
    symbols: Dict[str, int] = base_symbols()
    pc = 0

    for line_number, raw_line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        line = strip_comment(raw_line).strip()
        if not line:
            continue
        tokens = tokenize(line, line_number)
        if not tokens:
            continue
        tokens = [token.lower() if not (token.startswith("'") or token.startswith('"')) else token
                  for token in tokens]

        # Definitions: define NAME VALUE
        if tokens[0] == "define":
            if len(tokens) != 3:
                raise ValueError(f"Line {line_number}: expected 'define name value'")
            name = tokens[1].lower()
            if name in symbols:
                raise ValueError(f"Line {line_number}: duplicate definition {name!r}")
            symbols[name] = parse_number(tokens[2], symbols, line_number)
            continue

        # Labels: .label or .label INSTRUCTION ...
        if tokens[0].startswith("."):
            label = tokens.pop(0)
            if label in symbols:
                raise ValueError(f"Line {line_number}: duplicate label {label!r}")
            symbols[label] = pc
            if not tokens:
                continue

        instructions.append(SourceInstruction(tokens=tokens, line_number=line_number))
        pc += 1
        if pc > INSTRUCTION_LIMIT:
            raise ValueError(f"Line {line_number}: program exceeds {INSTRUCTION_LIMIT} instructions")

    return instructions, symbols


def encode_instruction(tokens: Sequence[str], symbols: Dict[str, int], line_number: int) -> int:
    if not tokens:
        raise ValueError(f"Line {line_number}: empty instruction")
    mnemonic = tokens[0].lower()
    args = list(tokens[1:])

    # Expand convenience mnemonics into instructions the current hardware has.
    if mnemonic == "cmp":
        if len(args) != 2:
            raise ValueError(f"Line {line_number}: CMP syntax is CMP Read1, Read2")
        args = [args[0], args[1], "r0"]
        mnemonic = "sub"
    elif mnemonic == "mov":
        if len(args) != 2:
            raise ValueError(f"Line {line_number}: MOV syntax is MOV source, destination")
        args = [args[0], "r0", args[1]]
        mnemonic = "add"
    elif mnemonic == "not":
        if len(args) != 2:
            raise ValueError(f"Line {line_number}: NOT syntax is NOT source, destination")
        args = [args[0], "r0", args[1]]
        mnemonic = "nor"
    elif mnemonic == "lshift":
        if len(args) == 1:
            args = [args[0], args[0], args[0]]
        elif len(args) == 2:
            args = [args[0], args[0], args[1]]
        else:
            raise ValueError(f"Line {line_number}: LSHIFT syntax is LSHIFT register [destination]")
        mnemonic = "add"
    elif mnemonic == "inc":
        if len(args) != 1:
            raise ValueError(f"Line {line_number}: INC syntax is INC register")
        args = [args[0], "1", args[0]]
        mnemonic = "adi"
    elif mnemonic == "dec":
        if len(args) != 1:
            raise ValueError(f"Line {line_number}: DEC syntax is DEC register")
        args = [args[0], "-1", args[0]]
        mnemonic = "adi"
    elif mnemonic == "neg":
        if len(args) == 1:
            args = ["r0", args[0], args[0]]
        elif len(args) == 2:
            args = ["r0", args[0], args[1]]
        else:
            raise ValueError(f"Line {line_number}: NEG syntax is NEG source [destination]")
        mnemonic = "sub"

    if mnemonic not in OPCODES:
        raise ValueError(f"Line {line_number}: unsupported instruction {mnemonic!r}")

    opcode = OPCODES[mnemonic]
    read1 = read2 = write = immediate = address = condition = 0

    if mnemonic in {"nop", "hlt"}:
        if args:
            raise ValueError(f"Line {line_number}: {mnemonic.upper()} takes no operands")

    elif mnemonic in {"add", "sub", "or", "nor", "and", "nand", "xor", "xnor"}:
        if len(args) != 3:
            raise ValueError(f"Line {line_number}: {mnemonic.upper()} syntax is {mnemonic.upper()} Read1, Read2, Write")
        read1 = parse_register(args[0], line_number)
        read2 = parse_register(args[1], line_number)
        write = parse_register(args[2], line_number)

    elif mnemonic == "rshift":
        if len(args) == 1:
            read1 = write = parse_register(args[0], line_number)
        elif len(args) == 2:
            read1 = parse_register(args[0], line_number)
            write = parse_register(args[1], line_number)
        else:
            raise ValueError(f"Line {line_number}: RSHIFT syntax is RSHIFT source [destination]")

    elif mnemonic == "ldi":
        if len(args) != 2:
            raise ValueError(f"Line {line_number}: LDI syntax is LDI destination, immediate")
        # LDI routes immediate data to the Read1-side destination path.
        read1 = parse_register(args[0], line_number)
        immediate = parse_number(args[1], symbols, line_number)
        if not IMMEDIATE_MIN <= immediate <= IMMEDIATE_MAX:
            raise ValueError(f"Line {line_number}: immediate must be between {IMMEDIATE_MIN} and {IMMEDIATE_MAX}")

    elif mnemonic == "adi":
        if len(args) == 2:
            source_reg = destination_reg = parse_register(args[0], line_number)
            immediate = parse_number(args[1], symbols, line_number)
        elif len(args) == 3:
            source_reg = parse_register(args[0], line_number)
            immediate = parse_number(args[1], symbols, line_number)
            destination_reg = parse_register(args[2], line_number)
        else:
            raise ValueError(f"Line {line_number}: ADI syntax is ADI Read1, immediate [, Write]")
        if not IMMEDIATE_MIN <= immediate <= IMMEDIATE_MAX:
            raise ValueError(f"Line {line_number}: immediate must be between {IMMEDIATE_MIN} and {IMMEDIATE_MAX}")
        read1 = source_reg
        write = destination_reg

    elif mnemonic in {"jmp", "cal"}:
        if len(args) != 1:
            raise ValueError(f"Line {line_number}: {mnemonic.upper()} syntax is {mnemonic.upper()} label_or_address")
        address = parse_address(args[0], symbols, line_number)

    elif mnemonic == "brh":
        if len(args) != 2:
            raise ValueError(f"Line {line_number}: BRH syntax is BRH condition, label_or_address (eq ne ge lt)")
        condition = parse_number(args[0], symbols, line_number)
        if args[0].lower() not in CONDITIONS:
            raise ValueError(f"Line {line_number}: unknown condition {args[0]!r}; use eq, ne, ge or lt")
        address = parse_address(args[1], symbols, line_number)

    elif mnemonic == "ret":
        if args:
            raise ValueError(f"Line {line_number}: RET takes no operands")

    elif mnemonic in {"lod", "str"}:
        # No offset: the memory address is exactly the value in the address register.
        if len(args) == 3 and parse_number(args[2], symbols, line_number) == 0:
            args = args[:2]
        if len(args) != 2:
            raise ValueError(f"Line {line_number}: {mnemonic.upper()} syntax is "
                             f"{mnemonic.upper()} address_register, {'destination' if mnemonic == 'lod' else 'data'}_register (no offset)")
        read1 = parse_register(args[0], line_number)      # address register
        if mnemonic == "lod":
            write = parse_register(args[1], line_number)  # LOD: memory -> this register
        else:
            read2 = parse_register(args[1], line_number)  # STR: this register -> memory

    # Field positions are fixed by the current 32-bit ISA proposal.
    word = (opcode << 27) | (read1 << 22) | (read2 << 17) | (write << 12) | ((immediate & 0xFF) << 4)
    if mnemonic in {"jmp", "cal", "brh"}:
        word |= address << ADDRESS_SHIFT
    if mnemonic == "brh":
        word |= condition << CONDITION_SHIFT
    if not 0 <= word < (1 << WORD_BITS):
        raise ValueError(f"Line {line_number}: encoded instruction does not fit in 32 bits")
    return word


def assemble(assembly_filename: str, mc_filename: str) -> None:
    instructions, symbols = parse_source(assembly_filename)
    machine_words: List[str] = []
    for instruction in instructions:
        word = encode_instruction(instruction.tokens, symbols, instruction.line_number)
        machine_words.append(f"{word:0{WORD_BITS}b}")

    output = Path(mc_filename)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(machine_words) + ("\n" if machine_words else ""), encoding="utf-8")
    print(f"Assembled {len(machine_words)} instruction(s) into {output}")


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: python assembler.py input.as [output.mc]")
        return 2
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) == 3 else str(Path(input_file).with_suffix(".mc"))
    try:
        assemble(input_file, output_file)
    except (OSError, ValueError) as exc:
        print(f"Assembler error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

