#!/usr/bin/env python3
"""Validate the pinned RMX1901 recovery kernel and the built boot image."""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
import zlib
from pathlib import Path


FDT_MAGIC = b"\xd0\r\xfe\xed"
BOOT_MAGIC = b"ANDROID!"
AVB_FOOTER_MAGIC = b"AVBf"
BOOT_HEADER_V1_SIZE = 1648


class ValidationError(ValueError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_kernel_dtb(image: bytes) -> tuple[bytes, bytes]:
    if not image.startswith(b"\x1f\x8b"):
        raise ValidationError("kernel does not start with a gzip header")

    reader = zlib.decompressobj(31)
    try:
        reader.decompress(image)
        reader.flush()
    except zlib.error as error:
        raise ValidationError(f"kernel gzip stream is invalid: {error}") from error

    if not reader.eof:
        raise ValidationError("kernel gzip stream is truncated")
    if not reader.unused_data:
        raise ValidationError("kernel has no appended DTB group")
    return image[: -len(reader.unused_data)], reader.unused_data


def validate_dtb_group(group: bytes) -> list[int]:
    sizes: list[int] = []
    offset = 0

    while offset < len(group):
        if len(group) - offset < 40:
            raise ValidationError(f"truncated DTB header at offset {offset}")
        if group[offset : offset + 4] != FDT_MAGIC:
            raise ValidationError(f"invalid DTB magic at offset {offset}")

        total_size = struct.unpack_from(">I", group, offset + 4)[0]
        if total_size < 40 or offset + total_size > len(group):
            raise ValidationError(
                f"invalid DTB size {total_size} at offset {offset}"
            )
        sizes.append(total_size)
        offset += total_size

    if not sizes:
        raise ValidationError("empty DTB group")
    return sizes


def validate_kernel(path: Path, expected_sha256: str | None = None) -> None:
    image = path.read_bytes()
    actual_sha256 = sha256(image)
    if expected_sha256 and actual_sha256 != expected_sha256.lower():
        raise ValidationError(
            f"kernel SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}"
        )

    gzip_image, dtb_group = split_kernel_dtb(image)
    dtb_sizes = validate_dtb_group(dtb_group)
    print(
        f"kernel OK: sha256={actual_sha256} gzip={len(gzip_image)} "
        f"dtbs={len(dtb_sizes)} dtb_bytes={sum(dtb_sizes)}"
    )


def align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def u32(image: bytes, offset: int) -> int:
    return struct.unpack_from("<I", image, offset)[0]


def u64(image: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", image, offset)[0]


def validate_recovery_image(
    path: Path, kernel_path: Path, dtbo_path: Path, partition_size: int
) -> None:
    image = path.read_bytes()
    kernel = kernel_path.read_bytes()
    dtbo = dtbo_path.read_bytes()

    if len(image) != partition_size:
        raise ValidationError(
            f"recovery image size is {len(image)}, expected {partition_size}"
        )
    if len(image) < BOOT_HEADER_V1_SIZE or image[:8] != BOOT_MAGIC:
        raise ValidationError("recovery image has no Android boot header")

    page_size = u32(image, 36)
    header_version = u32(image, 40)
    kernel_size = u32(image, 8)
    ramdisk_size = u32(image, 16)
    recovery_dtbo_size = u32(image, 1632)
    recovery_dtbo_offset = u64(image, 1636)
    header_size = u32(image, 1644)

    if page_size == 0 or page_size & (page_size - 1):
        raise ValidationError(f"invalid page size {page_size}")
    if header_version != 1 or header_size != BOOT_HEADER_V1_SIZE:
        raise ValidationError(
            f"expected boot header v1/{BOOT_HEADER_V1_SIZE}, got "
            f"v{header_version}/{header_size}"
        )

    kernel_offset = page_size
    ramdisk_offset = align(kernel_offset + kernel_size, page_size)
    if image[kernel_offset : kernel_offset + kernel_size] != kernel:
        raise ValidationError("recovery image does not contain the pinned kernel")
    if kernel_size != len(kernel):
        raise ValidationError(
            f"kernel size is {kernel_size}, expected {len(kernel)}"
        )
    if ramdisk_size == 0:
        raise ValidationError("recovery ramdisk is empty")
    if ramdisk_offset + ramdisk_size > len(image):
        raise ValidationError("recovery ramdisk extends past the image")

    if recovery_dtbo_size != len(dtbo):
        raise ValidationError(
            f"recovery DTBO size is {recovery_dtbo_size}, expected {len(dtbo)}"
        )
    if image[recovery_dtbo_offset : recovery_dtbo_offset + recovery_dtbo_size] != dtbo:
        raise ValidationError("recovery image does not contain the pinned DTBO")
    if image[-64:-60] != AVB_FOOTER_MAGIC:
        raise ValidationError("recovery image has no AVB footer")

    print(
        f"recovery image OK: sha256={sha256(image)} kernel={kernel_size} "
        f"ramdisk={ramdisk_size} dtbo={recovery_dtbo_size}"
    )


def parser() -> argparse.ArgumentParser:
    command_parser = argparse.ArgumentParser(description=__doc__)
    subparsers = command_parser.add_subparsers(dest="command", required=True)

    kernel_parser = subparsers.add_parser("kernel")
    kernel_parser.add_argument("path", type=Path)
    kernel_parser.add_argument("--expected-sha256")

    image_parser = subparsers.add_parser("image")
    image_parser.add_argument("path", type=Path)
    image_parser.add_argument("--kernel", required=True, type=Path)
    image_parser.add_argument("--dtbo", required=True, type=Path)
    image_parser.add_argument("--partition-size", required=True, type=int)
    return command_parser


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "kernel":
            validate_kernel(args.path, args.expected_sha256)
        else:
            validate_recovery_image(
                args.path, args.kernel, args.dtbo, args.partition_size
            )
    except (OSError, ValidationError) as error:
        print(f"recovery image validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
