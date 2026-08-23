#!/usr/bin/env python3
"""Android Binary XML (ABX) converter and rescue utility for Recovery environments.

Supports bidirectional conversion between Android 12-16 Binary XML (ABX)
and standard plain-text XML.
"""

from __future__ import annotations

import argparse
import base64
import io
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import BinaryIO

ABX_MAGIC = b"ABX\x00"

# Token constants (lower 4 bits)
START_DOCUMENT = 0
END_DOCUMENT = 1
START_TAG = 2
END_TAG = 3
TEXT = 4
CDSECT = 5
ENTITY_REF = 6
IGNORABLE_WHITESPACE = 7
PROCESSING_INSTRUCTION = 8
COMMENT = 9
DOCDECL = 10
ATTRIBUTE = 15

# Type constants (upper 4 bits)
TYPE_NULL = 1
TYPE_STRING = 2
TYPE_STRING_INTERNED = 3
TYPE_BYTES_HEX = 4
TYPE_BYTES_BASE64 = 5
TYPE_INT = 6
TYPE_INT_HEX = 7
TYPE_LONG = 8
TYPE_LONG_HEX = 9
TYPE_FLOAT = 10
TYPE_DOUBLE = 11
TYPE_BOOLEAN_TRUE = 12
TYPE_BOOLEAN_FALSE = 13


def is_abx(data: bytes) -> bool:
    """Check if the provided byte stream starts with the ABX magic signature."""
    return bool(data and data.startswith(ABX_MAGIC))


class FastDataInput:
    """Stream reader supporting Android Binary XML data formats and string interning."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.strings: list[str] = []

    def read_bytes(self, count: int) -> bytes:
        data = self.stream.read(count)
        if len(data) < count:
            raise EOFError(f"Unexpected end of stream: expected {count} bytes, got {len(data)}")
        return data

    def read_byte(self) -> int:
        return self.read_bytes(1)[0]

    def read_short(self) -> int:
        return struct.unpack(">H", self.read_bytes(2))[0]

    def read_int(self) -> int:
        return struct.unpack(">i", self.read_bytes(4))[0]

    def read_long(self) -> int:
        return struct.unpack(">q", self.read_bytes(8))[0]

    def read_float(self) -> float:
        return struct.unpack(">f", self.read_bytes(4))[0]

    def read_double(self) -> float:
        return struct.unpack(">d", self.read_bytes(8))[0]

    def read_utf(self) -> str:
        length = self.read_short()
        raw = self.read_bytes(length)
        return raw.decode("utf-8")

    def read_interned_utf(self) -> str:
        ref = self.read_short()
        if ref == 0xFFFF:
            s = self.read_utf()
            self.strings.append(s)
            return s
        if 0 <= ref < len(self.strings):
            return self.strings[ref]
        raise ValueError(f"Invalid string intern reference: {ref} (pool size: {len(self.strings)})")

    def peek_byte(self) -> int | None:
        pos = self.stream.tell()
        data = self.stream.read(1)
        self.stream.seek(pos)
        return data[0] if data else None


class FastDataOutput:
    """Stream writer supporting Android Binary XML data formats and string interning."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.string_refs: dict[str, int] = {}

    def write_bytes(self, data: bytes) -> None:
        self.stream.write(data)

    def write_byte(self, val: int) -> None:
        self.stream.write(bytes([val & 0xFF]))

    def write_short(self, val: int) -> None:
        self.stream.write(struct.pack(">H", val & 0xFFFF))

    def write_int(self, val: int) -> None:
        self.stream.write(struct.pack(">i", val))

    def write_long(self, val: int) -> None:
        self.stream.write(struct.pack(">q", val))

    def write_float(self, val: float) -> None:
        self.stream.write(struct.pack(">f", val))

    def write_double(self, val: float) -> None:
        self.stream.write(struct.pack(">d", val))

    def write_utf(self, s: str) -> None:
        data = s.encode("utf-8")
        if len(data) > 0xFFFF:
            raise ValueError(f"String length {len(data)} exceeds max unsigned short (65535)")
        self.write_short(len(data))
        self.write_bytes(data)

    def write_interned_utf(self, s: str) -> None:
        if s in self.string_refs:
            self.write_short(self.string_refs[s])
        else:
            self.write_short(0xFFFF)
            self.write_utf(s)
            self.string_refs[s] = len(self.string_refs)


def abx2xml(data: bytes) -> str:
    """Decode Android Binary XML (ABX) binary payload into a formatted XML string."""
    if not is_abx(data):
        raise ValueError("Provided data is not a valid Android Binary XML (missing ABX header)")

    stream = io.BytesIO(data[4:])
    reader = FastDataInput(stream)

    lines: list[str] = ['<?xml version="1.0" encoding="utf-8"?>']
    indent = 0

    while True:
        b = reader.peek_byte()
        if b is None:
            break
        b = reader.read_byte()
        token = b & 0x0F
        type_tag = b >> 4

        if token == START_DOCUMENT:
            continue
        elif token == END_DOCUMENT:
            break
        elif token == START_TAG:
            if type_tag == TYPE_STRING_INTERNED:
                tag_name = reader.read_interned_utf()
            else:
                tag_name = reader.read_utf()

            attrs: list[tuple[str, str]] = []
            while True:
                next_b = reader.peek_byte()
                if next_b is None or (next_b & 0x0F) != ATTRIBUTE:
                    break
                next_b = reader.read_byte()
                attr_type = next_b >> 4
                attr_name = reader.read_interned_utf()

                if attr_type == TYPE_STRING:
                    attr_val = reader.read_utf()
                elif attr_type == TYPE_STRING_INTERNED:
                    attr_val = reader.read_interned_utf()
                elif attr_type == TYPE_INT:
                    attr_val = str(reader.read_int())
                elif attr_type == TYPE_INT_HEX:
                    attr_val = hex(reader.read_int())
                elif attr_type == TYPE_LONG:
                    attr_val = str(reader.read_long())
                elif attr_type == TYPE_LONG_HEX:
                    attr_val = hex(reader.read_long())
                elif attr_type == TYPE_FLOAT:
                    attr_val = str(reader.read_float())
                elif attr_type == TYPE_DOUBLE:
                    attr_val = str(reader.read_double())
                elif attr_type == TYPE_BOOLEAN_TRUE:
                    attr_val = "true"
                elif attr_type == TYPE_BOOLEAN_FALSE:
                    attr_val = "false"
                elif attr_type == TYPE_BYTES_HEX:
                    raw_len = reader.read_short()
                    attr_val = reader.read_bytes(raw_len).hex()
                elif attr_type == TYPE_BYTES_BASE64:
                    raw_len = reader.read_short()
                    attr_val = base64.b64encode(reader.read_bytes(raw_len)).decode("ascii")
                elif attr_type == TYPE_NULL:
                    attr_val = ""
                else:
                    attr_val = ""

                attrs.append((attr_name, attr_val))

            attr_str = "".join(f' {k}="{v}"' for k, v in attrs)
            lines.append("    " * indent + f"<{tag_name}{attr_str}>")
            indent += 1

        elif token == TEXT:
            if type_tag == TYPE_STRING_INTERNED:
                text_content = reader.read_interned_utf()
            else:
                text_content = reader.read_utf()
            if text_content.strip():
                lines.append("    " * indent + text_content.strip())

        elif token == END_TAG:
            indent = max(0, indent - 1)
            if type_tag == TYPE_STRING_INTERNED:
                tag_name = reader.read_interned_utf()
            else:
                tag_name = reader.read_utf()
            lines.append("    " * indent + f"</{tag_name}>")

        else:
            raise ValueError(f"Invalid or unexpected ABX token: {token} (byte: {b:#04x})")

    return "\n".join(lines) + "\n"


def _encode_element(writer: FastDataOutput, elem: ET.Element) -> None:
    """Encode an XML ElementTree node into ABX stream."""
    writer.write_byte(START_TAG | (TYPE_STRING_INTERNED << 4))
    writer.write_interned_utf(elem.tag)

    # Encode attributes with type inference
    for attr_name, attr_val in elem.attrib.items():
        if attr_val.lower() == "true":
            writer.write_byte(ATTRIBUTE | (TYPE_BOOLEAN_TRUE << 4))
            writer.write_interned_utf(attr_name)
        elif attr_val.lower() == "false":
            writer.write_byte(ATTRIBUTE | (TYPE_BOOLEAN_FALSE << 4))
            writer.write_interned_utf(attr_name)
        elif attr_val.startswith("0x") or attr_val.startswith("0X"):
            try:
                val = int(attr_val, 16)
                if -2147483648 <= val <= 2147483647:
                    writer.write_byte(ATTRIBUTE | (TYPE_INT_HEX << 4))
                    writer.write_interned_utf(attr_name)
                    writer.write_int(val)
                else:
                    writer.write_byte(ATTRIBUTE | (TYPE_LONG_HEX << 4))
                    writer.write_interned_utf(attr_name)
                    writer.write_long(val)
                continue
            except ValueError:
                pass
        else:
            try:
                val = int(attr_val)
                if -2147483648 <= val <= 2147483647:
                    writer.write_byte(ATTRIBUTE | (TYPE_INT << 4))
                    writer.write_interned_utf(attr_name)
                    writer.write_int(val)
                else:
                    writer.write_byte(ATTRIBUTE | (TYPE_LONG << 4))
                    writer.write_interned_utf(attr_name)
                    writer.write_long(val)
                continue
            except ValueError:
                pass

            # Fallback to string attribute
            writer.write_byte(ATTRIBUTE | (TYPE_STRING << 4))
            writer.write_interned_utf(attr_name)
            writer.write_utf(attr_val)

    # Text content
    if elem.text and elem.text.strip():
        writer.write_byte(TEXT | (TYPE_STRING << 4))
        writer.write_utf(elem.text.strip())

    # Children
    for child in elem:
        _encode_element(writer, child)

    # End tag
    writer.write_byte(END_TAG | (TYPE_STRING_INTERNED << 4))
    writer.write_interned_utf(elem.tag)


def xml2abx(xml_str: str) -> bytes:
    """Encode an XML string into standard Android Binary XML (ABX) payload."""
    root = ET.fromstring(xml_str)
    out = io.BytesIO()
    writer = FastDataOutput(out)

    writer.write_bytes(ABX_MAGIC)
    writer.write_byte(START_DOCUMENT | (TYPE_NULL << 4))

    _encode_element(writer, root)

    writer.write_byte(END_DOCUMENT | (TYPE_NULL << 4))
    return out.getvalue()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Android Binary XML (ABX) bidirectional converter and recovery tool."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # decode command
    decode_p = subparsers.add_parser("decode", help="Decode an ABX file to readable XML")
    decode_p.add_argument("input", type=Path, help="Input ABX file (or - for stdin)")
    decode_p.add_argument("output", type=Path, nargs="?", help="Output XML file (or stdout)")

    # encode command
    encode_p = subparsers.add_parser("encode", help="Encode an XML file to Android Binary XML")
    encode_p.add_argument("input", type=Path, help="Input XML file (or - for stdin)")
    encode_p.add_argument("output", type=Path, nargs="?", help="Output ABX file (or stdout)")

    # info command
    info_p = subparsers.add_parser("info", help="Display ABX file metadata")
    info_p.add_argument("input", type=Path, help="Input file")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "decode":
            input_data = (
                sys.stdin.buffer.read()
                if str(args.input) == "-"
                else args.input.read_bytes()
            )
            if not is_abx(input_data):
                # If plain XML already, pass through
                xml_out = input_data.decode("utf-8", errors="replace")
            else:
                xml_out = abx2xml(input_data)

            if args.output and str(args.output) != "-":
                args.output.write_text(xml_out, encoding="utf-8")
            else:
                sys.stdout.write(xml_out)

        elif args.command == "encode":
            input_text = (
                sys.stdin.read()
                if str(args.input) == "-"
                else args.input.read_text(encoding="utf-8")
            )
            abx_out = xml2abx(input_text)
            if args.output and str(args.output) != "-":
                args.output.write_bytes(abx_out)
            else:
                sys.stdout.buffer.write(abx_out)

        elif args.command == "info":
            input_data = args.input.read_bytes()
            if is_abx(input_data):
                print(f"{args.input}: Valid Android Binary XML (ABX v0), size={len(input_data)} bytes")
            else:
                print(f"{args.input}: Plain-text XML or non-ABX data, size={len(input_data)} bytes")

    except Exception as e:
        print(f"abx-tool error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
