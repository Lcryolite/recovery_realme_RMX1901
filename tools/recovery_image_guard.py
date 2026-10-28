#!/usr/bin/env python3
"""Validate the pinned RMX1901 recovery kernel and the built boot image."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import lzma
import stat
import struct
import subprocess
import sys
import zlib
from pathlib import Path


FDT_MAGIC = b"\xd0\r\xfe\xed"
BOOT_MAGIC = b"ANDROID!"
AVB_FOOTER_MAGIC = b"AVBf"
BOOT_HEADER_V1_SIZE = 1648
# Observed RMX1901 C.02 ABL fallback layout, including its two page guards.
ABL_KERNEL_WINDOW_SIZE = 0x05600000
ABL_KERNEL_LOAD_OFFSET = 0x00080000
ABL_DTB_RESERVE = 0x00200000
ADB_READY_TRIGGER = frozenset(
    (
        b"property:sys.usb.config=adb",
        b"property:sys.usb.ffs.ready=1",
        b"property:sys.usb.configfs=1",
    )
)


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


def validate_ramdisk_support(raw_kernel: bytes, ramdisk: bytes) -> str:
    if ramdisk.startswith(b"\x1f\x8b"):
        compression = "gzip"
    elif ramdisk.startswith(b"\x02\x21\x4c\x18"):
        compression = "lz4"
    elif ramdisk.startswith(b"\x28\xb5\x2f\xfd"):
        compression = "zstd"
    elif ramdisk.startswith(b"\x5d\x00"):
        compression = "lzma"
    else:
        raise ValidationError("unsupported Recovery ramdisk compression")

    try:
        marker = b"IKCFG_ST\x1f\x8b\x08"
        offset = raw_kernel.find(marker)
        if offset < 0:
            raise ValidationError("kernel has no embedded IKCONFIG for ramdisk validation")
        reader = zlib.decompressobj(31)
        config = reader.decompress(raw_kernel[offset + 8 :]) + reader.flush()
    except zlib.error as error:
        raise ValidationError(f"invalid embedded kernel configuration: {error}") from error

    if not reader.eof or not reader.unused_data.startswith(b"IKCFG_ED"):
        raise ValidationError("embedded kernel configuration is truncated or invalid")
    enabled = set(config.splitlines())
    suffix = compression.upper().encode()
    required = [
        b"CONFIG_BLK_DEV_INITRD",
        b"CONFIG_RD_" + suffix,
        b"CONFIG_DECOMPRESS_" + suffix,
    ]
    if compression == "zstd":
        required.append(b"CONFIG_ZSTD_DECOMPRESS")
    missing = [option.decode() for option in required if option + b"=y" not in enabled]
    if missing:
        raise ValidationError(
            f"{compression} ramdisk requires {', '.join(missing)} in kernel configuration"
        )
    return compression


def decompress_ramdisk(ramdisk: bytes, compression: str) -> bytes:
    try:
        if compression == "gzip":
            return gzip.decompress(ramdisk)
        if compression == "lzma":
            return lzma.decompress(ramdisk, format=lzma.FORMAT_ALONE)
        # The kernel's Zstd decoder permits a window of at most 128 MiB.
        command = (
            ["zstd", "-d", "-c", "-M134217728"]
            if compression == "zstd"
            else ["lz4", "-d", "-c"]
        )
        result = subprocess.run(
            command,
            input=ramdisk,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (OSError, EOFError, lzma.LZMAError) as error:
        raise ValidationError(
            f"invalid {compression} Recovery ramdisk: {error}"
        ) from error
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise ValidationError(f"invalid {compression} Recovery ramdisk: {detail}")
    return result.stdout


def validate_ramdisk_applets(cpio: bytes) -> None:
    required: dict[bytes, tuple[int, bytes] | None] = {
        b"system/bin/toybox": None,
        b"system/bin/printf": None,
        b"system/bin/dmesg": None,
    }
    adb_ready_trigger_owners: list[bytes] = []
    offset = 0

    while True:
        if len(cpio) - offset < 110:
            raise ValidationError("Recovery ramdisk has a truncated newc header")
        header = cpio[offset : offset + 110]
        if header[:6] not in (b"070701", b"070702"):
            raise ValidationError(f"invalid newc magic at offset {offset}")
        try:
            fields = [
                int(header[index : index + 8], 16)
                for index in range(6, 110, 8)
            ]
        except ValueError as error:
            raise ValidationError(f"invalid newc header at offset {offset}") from error
        mode = fields[1]
        file_size = fields[6]
        name_size = fields[11]
        if name_size < 1:
            raise ValidationError(f"invalid newc name size at offset {offset}")

        name_start = offset + 110
        name_end = name_start + name_size
        if name_end > len(cpio) or cpio[name_end - 1] != 0:
            raise ValidationError(f"truncated newc name at offset {offset}")
        name = cpio[name_start : name_end - 1]
        data_start = align(name_end, 4)
        data_end = data_start + file_size
        if data_end > len(cpio):
            raise ValidationError(f"truncated newc data for {name!r}")
        data = cpio[data_start:data_end]

        if stat.S_IFMT(mode) == stat.S_IFREG and name.endswith(b".rc"):
            for line in data.splitlines():
                stripped = line.strip()
                if stripped.startswith(b"on ") and frozenset(
                    condition.strip()
                    for condition in stripped[3:].split(b" && ")
                ) == ADB_READY_TRIGGER:
                    adb_ready_trigger_owners.append(name)

        if name in required:
            if required[name] is not None:
                raise ValidationError(f"duplicate newc record for {name.decode()}")
            required[name] = (mode, data)
        if name == b"TRAILER!!!":
            if any(cpio[align(data_end, 4) :]):
                raise ValidationError("Recovery ramdisk has data after the newc trailer")
            break
        offset = align(data_end, 4)

    missing = [name.decode() for name, record in required.items() if record is None]
    if missing:
        raise ValidationError(f"Recovery ramdisk is missing {', '.join(missing)}")
    if len(adb_ready_trigger_owners) > 1:
        owners = ", ".join(name.decode() for name in adb_ready_trigger_owners)
        raise ValidationError(
            f"ADB FunctionFS ready trigger has multiple owners: {owners}"
        )

    toybox_mode, toybox = required[b"system/bin/toybox"]
    if (
        stat.S_IFMT(toybox_mode) != stat.S_IFREG
        or not toybox_mode & 0o111
        or not toybox.startswith(b"\x7fELF")
    ):
        raise ValidationError("system/bin/toybox must be a regular executable ELF")
    for name in (b"system/bin/printf", b"system/bin/dmesg"):
        mode, target = required[name]
        if stat.S_IFMT(mode) != stat.S_IFLNK or target != b"toybox":
            raise ValidationError(f"{name.decode()} must be a symlink to toybox")


def validate_abl_workspace(
    raw_kernel: bytes, kernel_size: int, ramdisk_size: int, page_size: int
) -> tuple[int, int]:
    if page_size != 4096:
        raise ValidationError("the verified RMX1901 ABL layout requires 4096-byte pages")
    if len(raw_kernel) < 64 or raw_kernel[56:60] != b"ARMd":
        raise ValidationError("kernel has no ARM64 Image header")
    runtime_size = u64(raw_kernel, 16)
    if runtime_size == 0:
        raise ValidationError("ARM64 kernel runtime size is unavailable")
    workspace = (
        ABL_KERNEL_WINDOW_SIZE
        - ABL_KERNEL_LOAD_OFFSET
        - align(ramdisk_size, page_size)
        - ABL_DTB_RESERVE
        - 2 * page_size
    )
    if kernel_size >= workspace or len(raw_kernel) > workspace:
        raise ValidationError("compressed or inflated kernel does not fit ABL workspace")
    if runtime_size > workspace:
        raise ValidationError(
            f"kernel runtime size {runtime_size} exceeds ABL workspace {workspace}"
        )
    return runtime_size, workspace


def validate_disabled_preinit(image: bytes) -> None:
    cmdline = image[64:576].split(b"\0", 1)[0]
    cmdline += image[608:1632].split(b"\0", 1)[0]
    arguments = cmdline.split()
    matches = [
        i
        for i, arg in enumerate(arguments)
        if arg == b"kernelsu.preinit" or arg.startswith(b"kernelsu.preinit=")
    ]
    if (
        len(matches) != 1
        or arguments[matches[0]] != b"kernelsu.preinit=0"
        or b"--" in arguments[: matches[0]]
    ):
        raise ValidationError("Recovery must disable BakaSU preinit exactly once")


def validate_recovery_image(
    path: Path,
    kernel_path: Path,
    dtbo_path: Path,
    partition_size: int,
    expected_ramdisk_format: str | None = None,
    require_disabled_preinit: bool = False,
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
    if require_disabled_preinit:
        validate_disabled_preinit(image)

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

    try:
        raw_kernel = zlib.decompress(kernel, 31)
    except zlib.error as error:
        raise ValidationError(f"kernel gzip stream is invalid: {error}") from error
    compression = validate_ramdisk_support(
        raw_kernel, image[ramdisk_offset : ramdisk_offset + ramdisk_size]
    )
    if expected_ramdisk_format and compression != expected_ramdisk_format:
        raise ValidationError(
            f"recovery ramdisk format is {compression}, expected {expected_ramdisk_format}"
        )
    ramdisk = image[ramdisk_offset : ramdisk_offset + ramdisk_size]
    validate_ramdisk_applets(decompress_ramdisk(ramdisk, compression))
    runtime_size, workspace = validate_abl_workspace(
        raw_kernel, kernel_size, ramdisk_size, page_size
    )
    print(
        f"recovery image OK: sha256={sha256(image)} kernel={kernel_size} "
        f"ramdisk={ramdisk_size} ramdisk_format={compression} dtbo={recovery_dtbo_size} "
        f"kernel_runtime={runtime_size} abl_workspace={workspace} "
        f"abl_headroom={workspace - runtime_size}"
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
    image_parser.add_argument(
        "--ramdisk-format", choices=("gzip", "lz4", "lzma", "zstd")
    )
    image_parser.add_argument("--require-disabled-preinit", action="store_true")
    return command_parser


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "kernel":
            validate_kernel(args.path, args.expected_sha256)
        else:
            validate_recovery_image(
                args.path, args.kernel, args.dtbo, args.partition_size, args.ramdisk_format,
                require_disabled_preinit=args.require_disabled_preinit,
            )
    except (OSError, ValidationError) as error:
        print(f"recovery image validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
