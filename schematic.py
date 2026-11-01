"""Create a sparse WorldEdit schematic that updates ONLY the 32-bit instruction cells.

Usage:
    python schematic.py program.mc program_patch.schem

Paste with WorldEdit while standing at the configured anchor:
    //schem load program_patch
    //paste -a

IMPORTANT: -a is required. Air in the sparse clipboard is intentionally used as
transparent space; -a makes WorldEdit leave every non-instruction block alone.
"""
from __future__ import annotations

import argparse
import gzip
import struct
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Instruction-memory layout reconstructed from the user's uploaded schematic.
# These are coordinates in the ORIGINAL selection's local coordinate system.
# ---------------------------------------------------------------------------

MEMORY_WIDTH = 1024
INSTRUCTION_BITS = 32

BASE_X = 126
BASE_Z = 144
HALF_X_OFFSET = 2
ADDRESS_X_STEP = 7
# Rows verified against your real build: every cell has red wool below it and
# redstone dust on BOTH its east and west side (the old 113/95/77/59 rows had
# glass on one side and no support under the last bit of each byte).
BYTE_GROUP_TOP_Y = (114, 96, 78, 60)  # Bits 31..24, 23..16, 15..8, 7..0
BIT_Y_STEP = 2

# Repeater "facing" = the side the INPUT comes from. In your build the address
# line is on the OUTER side of each cell and the bit line is the shared dust
# line in the MIDDLE, so the two halves are mirror images:
#   addresses 0-511    (east of the middle line): facing=east
#   addresses 512-1023 (west of the middle line): facing=west
FIRST_HALF_FACING = "east"
SECOND_HALF_FACING = "west"

ZERO_BLOCK = "minecraft:red_wool"
EAST_REPEATER = "minecraft:repeater[delay=1,facing=east,locked=false,powered=false]"
WEST_REPEATER = "minecraft:repeater[delay=1,facing=west,locked=false,powered=false]"

# Paste-anchor relationship to the address-0, bit-31 block.
# Facing west means behind the player is east (+X), so stand 5 blocks east.
# Your feet should be one block above the bit-31 block of address 0.
# The old value (1) landed one block too LOW in your world, so it is now 0.
# Z: confirmed - the redstone line is directly in front of you (no offset).
ANCHOR_DX_FROM_ADDRESS0 = 5
ANCHOR_DY_FROM_ADDRESS0 = 0
ANCHOR_DZ_FROM_ADDRESS0 = 0

# Use the DataVersion from the user's current template for compatibility.
DEFAULT_TEMPLATE = "25%progress.schem"
DEFAULT_DATA_VERSION = 4671

# Sponge schematic NBT tag IDs
TAG_END = 0
TAG_BYTE = 1
TAG_SHORT = 2
TAG_INT = 3
TAG_LONG = 4
TAG_BYTE_ARRAY = 7
TAG_STRING = 8
TAG_LIST = 9
TAG_COMPOUND = 10
TAG_INT_ARRAY = 11


def _read_u8(data: bytes, pos: int) -> tuple[int, int]:
    return data[pos], pos + 1


def _read_string(data: bytes, pos: int) -> tuple[str, int]:
    n = struct.unpack_from(">H", data, pos)[0]
    pos += 2
    return data[pos:pos+n].decode("utf-8"), pos + n


def _read_payload(data: bytes, pos: int, tag_type: int) -> tuple[Any, int]:
    if tag_type == TAG_BYTE:
        return struct.unpack_from(">b", data, pos)[0], pos + 1
    if tag_type == TAG_SHORT:
        return struct.unpack_from(">h", data, pos)[0], pos + 2
    if tag_type == TAG_INT:
        return struct.unpack_from(">i", data, pos)[0], pos + 4
    if tag_type == TAG_LONG:
        return struct.unpack_from(">q", data, pos)[0], pos + 8
    if tag_type == TAG_BYTE_ARRAY:
        n = struct.unpack_from(">i", data, pos)[0]
        pos += 4
        return data[pos:pos+n], pos + n
    if tag_type == TAG_STRING:
        return _read_string(data, pos)
    if tag_type == TAG_INT_ARRAY:
        n = struct.unpack_from(">i", data, pos)[0]
        pos += 4
        values = list(struct.unpack_from(">" + "i" * n, data, pos)) if n else []
        return values, pos + 4*n
    if tag_type == TAG_LIST:
        elem_type = data[pos]
        n = struct.unpack_from(">i", data, pos + 1)[0]
        pos += 5
        items = []
        for _ in range(n):
            value, pos = _read_payload(data, pos, elem_type)
            items.append(value)
        return (elem_type, items), pos
    if tag_type == TAG_COMPOUND:
        items: dict[str, tuple[int, Any]] = {}
        while True:
            child_type = data[pos]
            pos += 1
            if child_type == TAG_END:
                break
            name, pos = _read_string(data, pos)
            value, pos = _read_payload(data, pos, child_type)
            items[name] = (child_type, value)
        return items, pos
    raise ValueError(f"Unsupported NBT tag type {tag_type} in template")


def read_template_data_version(path: Path | None) -> int:
    if path is None or not path.exists():
        return DEFAULT_DATA_VERSION
    raw = gzip.decompress(path.read_bytes())
    pos = 0
    root_type = raw[pos]
    pos += 1
    if root_type != TAG_COMPOUND:
        raise ValueError("Template has an invalid NBT root tag")
    _, pos = _read_string(raw, pos)  # Root name
    root, pos = _read_payload(raw, pos, TAG_COMPOUND)
    schematic_tag = root.get("Schematic")
    if not schematic_tag or schematic_tag[0] != TAG_COMPOUND:
        raise ValueError("Template is missing its Schematic compound")
    dv = schematic_tag[1].get("DataVersion")
    return int(dv[1]) if dv and dv[0] == TAG_INT else DEFAULT_DATA_VERSION


def address_position(address: int) -> tuple[int, int, str]:
    """Return original-selection local X, Z and repeater facing."""
    if not 0 <= address < MEMORY_WIDTH:
        raise ValueError(f"Address out of range: {address}")
    half = address // 512
    within_half = address % 512
    row = within_half // 16
    col = within_half % 16

    x = BASE_X - HALF_X_OFFSET * half - ADDRESS_X_STEP * col
    z = BASE_Z + 2 * row + (4 if row >= 16 else 0)
    if col % 2:
        z += 1 if row < 16 else -1

    facing = FIRST_HALF_FACING if half == 0 else SECOND_HALF_FACING
    return x, z, facing


def parse_machine_code(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"Machine-code file not found: {path}")
    instructions: list[str] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if len(line) != 32 or any(ch not in "01" for ch in line):
            raise ValueError(
                f"Line {line_no}: expected exactly 32 binary digits; got {line!r}"
            )
        instructions.append(line)
        if len(instructions) > MEMORY_WIDTH:
            raise ValueError("Program exceeds the 1,024-instruction memory capacity")
    instructions.extend(["0" * 32] * (MEMORY_WIDTH - len(instructions)))
    return instructions


def _write_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 65535:
        raise ValueError("NBT string is too long")
    return struct.pack(">H", len(encoded)) + encoded


def _tag(tag_type: int, name: str, payload: bytes) -> bytes:
    return bytes([tag_type]) + _write_string(name) + payload


def _compound_payload(children: list[bytes]) -> bytes:
    return b"".join(children) + bytes([TAG_END])


def _int_payload(value: int) -> bytes:
    return struct.pack(">i", value)


def _short_payload(value: int) -> bytes:
    return struct.pack(">h", value)


def _long_payload(value: int) -> bytes:
    return struct.pack(">q", value)


def _int_array_payload(values: list[int]) -> bytes:
    return struct.pack(">i", len(values)) + (struct.pack(">" + "i" * len(values), *values) if values else b"")


def _byte_array_payload(value: bytes) -> bytes:
    return struct.pack(">i", len(value)) + value


def _varint(value: int) -> bytes:
    out = bytearray()
    while value & ~0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _build_palette_and_data(instructions: list[str], only_addresses=None) -> tuple[int, int, int, int, bytes, tuple[int, int, int]]:
    positions: list[tuple[int, int, int, int]] = []  # Original local X,Y,Z and palette id
    min_x = min_z = min_y = 10**9
    max_x = max_y = max_z = -10**9
    repeater_count = 0

    for address, word in enumerate(instructions):
        if only_addresses is not None and address not in only_addresses:
            continue
        x, z, facing = address_position(address)
        repeater_id = 2 if facing == "west" else 3
        for group, top_y in enumerate(BYTE_GROUP_TOP_Y):
            byte = word[group * 8:(group + 1) * 8]
            for bit_index, ch in enumerate(byte):
                y = top_y - BIT_Y_STEP * bit_index
                state_id = repeater_id if ch == "1" else 1  # red wool for zero
                positions.append((x, y, z, state_id))
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
                min_z, max_z = min(min_z, z), max(max_z, z)
                if ch == "1":
                    repeater_count += 1

    width = max_x - min_x + 1
    height = max_y - min_y + 1
    length = max_z - min_z + 1
    volume = width * height * length
    palette_data = bytearray(volume)  # Palette 0 = air; transparent on //paste -a.

    for x, y, z, state_id in positions:
        lx, ly, lz = x - min_x, y - min_y, z - min_z
        # Sponge order: x fastest, then z, then y.
        index = (ly * length + lz) * width + lx
        palette_data[index] = state_id

    # Clipboard origin is the player's standing block. Address 0 bit 31 is
    # placed 5 west of the player and one block below their feet.
    address0_x, address0_z, _ = address_position(0)
    address0_bit31_y = BYTE_GROUP_TOP_Y[0]
    anchor_original = (
        address0_x + ANCHOR_DX_FROM_ADDRESS0,
        address0_bit31_y + ANCHOR_DY_FROM_ADDRESS0,
        address0_z + ANCHOR_DZ_FROM_ADDRESS0,
    )
    anchor_local = (
        anchor_original[0] - min_x,
        anchor_original[1] - min_y,
        anchor_original[2] - min_z,
    )

    # For Sponge v3, Offset = region minimum - clipboard origin. Region min is 0.
    offset = (-anchor_local[0], -anchor_local[1], -anchor_local[2])
    return width, height, length, repeater_count, bytes(palette_data), offset


def write_sparse_schematic(path: Path, data_version: int, width: int, height: int,
                            length: int, block_data: bytes, offset: tuple[int, int, int]) -> None:
    palette_entries = [
        _tag(TAG_INT, "minecraft:air", _int_payload(0)),
        _tag(TAG_INT, ZERO_BLOCK, _int_payload(1)),
        _tag(TAG_INT, WEST_REPEATER, _int_payload(2)),
        _tag(TAG_INT, EAST_REPEATER, _int_payload(3)),
    ]
    palette = _tag(TAG_COMPOUND, "Palette", _compound_payload(palette_entries))
    data_tag = _tag(TAG_BYTE_ARRAY, "Data", _byte_array_payload(block_data))
    empty_block_entities = _tag(TAG_LIST, "BlockEntities", bytes([TAG_COMPOUND]) + struct.pack(">i", 0))
    blocks = _tag(TAG_COMPOUND, "Blocks", _compound_payload([palette, data_tag, empty_block_entities]))

    # In Sponge v3, the WorldEdit Origin is the clipboard origin that is placed
    # at the player's position. Offset is min - origin. We choose min=(0,0,0).
    origin_x, origin_y, origin_z = (-offset[0], -offset[1], -offset[2])
    we_children = [
        _tag(TAG_STRING, "Version", _write_string("7.4.2")),
        _tag(TAG_STRING, "EditingPlatform", _write_string("enginehub:fabric")),
        _tag(TAG_INT_ARRAY, "Origin", _int_array_payload([origin_x, origin_y, origin_z])),
    ]
    worldedit = _tag(TAG_COMPOUND, "WorldEdit", _compound_payload(we_children))
    metadata = _tag(TAG_COMPOUND, "Metadata", _compound_payload([
        _tag(TAG_LONG, "Date", _long_payload(int(time.time() * 1000))),
        worldedit,
    ]))

    schematic_children = [
        _tag(TAG_INT, "Version", _int_payload(3)),
        _tag(TAG_INT, "DataVersion", _int_payload(data_version)),
        metadata,
        _tag(TAG_SHORT, "Width", _short_payload(width)),
        _tag(TAG_SHORT, "Height", _short_payload(height)),
        _tag(TAG_SHORT, "Length", _short_payload(length)),
        _tag(TAG_INT_ARRAY, "Offset", _int_array_payload(list(offset))),
        blocks,
    ]
    schematic = _tag(TAG_COMPOUND, "Schematic", _compound_payload(schematic_children))
    root_payload = _compound_payload([schematic])
    raw_nbt = bytes([TAG_COMPOUND]) + _write_string("") + root_payload
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(raw_nbt, compresslevel=6, mtime=0))


def make_schematic(mc_filename: str | Path, schem_filename: str | Path,
                   template_filename: str | Path | None = None,
                   only_addresses=None, anchor_dy=None) -> None:
    global ANCHOR_DY_FROM_ADDRESS0
    if anchor_dy is not None:
        ANCHOR_DY_FROM_ADDRESS0 = anchor_dy
    mc_path = Path(mc_filename)
    out_path = Path(schem_filename)
    template_path = Path(template_filename) if template_filename else Path(__file__).resolve().with_name(DEFAULT_TEMPLATE)
    instructions = parse_machine_code(mc_path)
    data_version = read_template_data_version(template_path)
    width, height, length, repeater_count, block_data, offset = _build_palette_and_data(instructions, only_addresses)
    write_sparse_schematic(out_path, data_version, width, height, length, block_data, offset)

    used = sum(word != "0" * 32 for word in instructions)
    print(f"Output schematic: {out_path}")
    print(f"Non-zero instructions: {used}")
    cells = (len(only_addresses) if only_addresses is not None else MEMORY_WIDTH) * INSTRUCTION_BITS
    print(f"Instruction cells represented: {cells:,}")
    print(f"Repeaters: {repeater_count:,}")
    print(f"Schematic dimensions: {width} x {height} x {length}")
    print("Paste with //paste -a (required: air is transparent). Do not rotate the clipboard.")
    print("Anchor: stand facing WEST, 5 blocks EAST of address 0, at player-feet Y = bit-31 block Y + 1.")
    print("Anchor Z: the redstone line is directly in front of you (no Z offset).")
    print(f"Sponge schematic offset: {offset}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate a sparse 32-bit instruction-memory patch schematic.")
    parser.add_argument("machine_code", help="32-bit binary machine-code file (.mc)")
    parser.add_argument("output_schematic", help="Output .schem file")
    parser.add_argument("--template", default=None, help="Optional original .schem used to copy DataVersion")
    parser.add_argument("--only-address", type=int, action="append", default=None,
                        help="Only write this address (repeatable). Use --only-address 0 for a tiny 32-block test patch.")
    parser.add_argument("--anchor-dy", type=int, default=None,
                        help="Height fine-tune without editing code. Default 0. If the patch lands one block too LOW, use -1; one block too HIGH, use 1.")
    args = parser.parse_args()
    only = set(args.only_address) if args.only_address else None
    make_schematic(args.machine_code, args.output_schematic, args.template, only, args.anchor_dy)

