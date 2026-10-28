#!/usr/bin/env python3
"""Create the 8 MiB SDSP comparison from an already-built Recovery image."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import recovery_image_guard as guard

SDSP_NODE = "/reserved-memory/sdsp_region"
PARTITION_SIZE = 67108864


def make_8m_kernel(kernel: bytes, expected_sha256: str, work: Path) -> bytes:
    gzip_image, group = guard.split_kernel_dtb(kernel)
    sizes = guard.validate_dtb_group(group)
    if len(sizes) != 4:
        raise guard.ValidationError(f"expected four DTBs, got {len(sizes)}")

    offset = 0
    changed = bytearray()
    for index, size in enumerate(sizes):
        original = group[offset : offset + size]
        dtb = work / f"{index}.dtb"
        dtb.write_bytes(original)
        values = subprocess.check_output(
            ["fdtget", "-t", "x", str(dtb), SDSP_NODE, "size"], text=True
        ).split()
        if [int(value, 16) for value in values] != [0, 0x400000]:
            raise guard.ValidationError(f"DTB {index} does not have a 4 MiB SDSP pool")
        subprocess.run(
            ["fdtput", "-t", "x", str(dtb), SDSP_NODE, "size", "0", "800000"],
            check=True,
        )
        updated = dtb.read_bytes()
        differences = [(a, b) for a, b in zip(original, updated) if a != b]
        if len(updated) != size or differences != [(0x40, 0x80)]:
            raise guard.ValidationError(f"unexpected changes in DTB {index}")
        changed.extend(updated)
        offset += size

    result = gzip_image + changed
    if guard.sha256(result) != expected_sha256.lower():
        raise guard.ValidationError("8 MiB kernel SHA-256 does not match the pinned output")
    return result


def read_avb(avb, path: Path):
    footer, header, descriptors, _ = avb.Avb()._parse_image(
        avb.ImageHandler(str(path), read_only=True)
    )
    hashes = [d for d in descriptors if isinstance(d, avb.AvbHashDescriptor)]
    properties = [d for d in descriptors if isinstance(d, avb.AvbPropertyDescriptor)]
    if (
        footer is None
        or len(hashes) != 1
        or descriptors != hashes + properties
    ):
        raise guard.ValidationError("unsupported AVB footer or descriptor layout")
    digest = hashes[0]
    if (
        header.algorithm_type != avb.ALGORITHMS["SHA256_RSA4096"].algorithm_type
        or header.public_key_metadata_size
        or digest.partition_name != "recovery"
        or digest.hash_algorithm != "sha256"
        or digest.flags
        or digest.image_size != footer.original_image_size
    ):
        raise guard.ValidationError("unsupported recovery AVB parameters")
    return footer, header, digest, properties


def avb_metadata(header, digest, properties):
    return (
        header.algorithm_type,
        header.rollback_index,
        header.rollback_index_location,
        header.flags,
        header.release_string,
        header.required_libavb_version_major,
        header.required_libavb_version_minor,
        digest.partition_name,
        digest.hash_algorithm,
        digest.flags,
        digest.image_size,
        digest.salt,
        [(prop.key, prop.value) for prop in properties],
    )


def verify_avb(avbtool: Path, image: Path, key: Path) -> None:
    # avbtool resolves the hash descriptor against a sibling named recovery.img.
    if image.name != "recovery.img":
        raise guard.ValidationError("AVB verification requires a file named recovery.img")
    subprocess.run(
        [sys.executable, str(avbtool), "verify_image", "--image", str(image), "--key", str(key)],
        check=True,
    )


def create_candidate(args) -> None:
    image = args.image.resolve()
    kernel_path = args.kernel.resolve()
    dtbo = args.dtbo.resolve()
    avbtool = args.avbtool.resolve()
    key = args.avb_key.resolve()
    output = args.output_dir.resolve()
    image_out = output / "recovery.img"
    kernel_out = output / "Image.gz-dtb"
    if image_out.exists() or kernel_out.exists():
        raise guard.ValidationError("comparison output already exists")

    kernel = kernel_path.read_bytes()
    if guard.sha256(kernel) != args.expected_kernel_sha256.lower():
        raise guard.ValidationError("default kernel SHA-256 does not match the pinned input")
    guard.validate_recovery_image(
        image, kernel_path, dtbo, PARTITION_SIZE, "zstd", require_disabled_preinit=True
    )
    verify_avb(avbtool, image, key)

    spec = importlib.util.spec_from_file_location("candidate_avbtool", avbtool)
    if spec is None or spec.loader is None:
        raise guard.ValidationError("cannot load the build's avbtool")
    avb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(avb)
    footer, header, digest, properties = read_avb(avb, image)
    metadata = avb_metadata(header, digest, properties)
    baseline = image.read_bytes()[: footer.original_image_size]

    with tempfile.TemporaryDirectory(prefix="sdsp-comparison-") as directory:
        work = Path(directory)
        parts = work / "parts"
        packed = work / "packed"
        parts.mkdir()
        packed.mkdir()
        unpacked = subprocess.check_output(
            [sys.executable, str(args.unpack_bootimg.resolve()), "--boot_img", str(image),
             "--out", str(parts), "--format=mkbootimg", "-0"]
        )
        if not unpacked.endswith(b"\0"):
            raise guard.ValidationError("unpack_bootimg did not return terminated arguments")
        mkbootimg_args = [value.decode() for value in unpacked.split(b"\0")[:-1]]
        extracted_kernel = parts / "kernel"
        if extracted_kernel.read_bytes() != kernel:
            raise guard.ValidationError("unpacked kernel differs from the pinned input")
        comparison_kernel = make_8m_kernel(kernel, args.expected_8m_sha256, work)
        extracted_kernel.write_bytes(comparison_kernel)

        candidate = packed / "recovery.img"
        subprocess.run(
            [str(args.mkbootimg.resolve()), *mkbootimg_args, "--output", str(candidate)],
            check=True,
        )
        unsigned = candidate.read_bytes()
        page_size = guard.u32(baseline, 36)
        kernel_end = page_size + len(kernel)
        if (
            len(unsigned) != len(baseline)
            or baseline[:576] != unsigned[:576]
            or baseline[608:page_size] != unsigned[608:page_size]
            or baseline[page_size:kernel_end] != kernel
            or unsigned[page_size:kernel_end] != comparison_kernel
            or baseline[kernel_end:] != unsigned[kernel_end:]
        ):
            raise guard.ValidationError("repacked image differs outside the DTBs and boot ID")

        sign = [
            sys.executable, str(avbtool), "add_hash_footer", "--image", str(candidate),
            "--partition_size", str(PARTITION_SIZE), "--partition_name", digest.partition_name,
            "--hash_algorithm", digest.hash_algorithm, "--salt", digest.salt.hex(),
            "--algorithm", "SHA256_RSA4096", "--key", str(key),
            "--rollback_index", str(header.rollback_index), "--flags", str(header.flags),
            "--rollback_index_location", str(header.rollback_index_location),
        ]
        for prop in properties:
            if ":" in prop.key or b"\0" in prop.value:
                raise guard.ValidationError("unsupported AVB property")
            sign.extend(["--prop", f"{prop.key}:{prop.value.decode('utf-8')}"])
        subprocess.run(sign, check=True)
        verify_avb(avbtool, candidate, key)
        _, new_header, new_digest, new_properties = read_avb(avb, candidate)
        if avb_metadata(new_header, new_digest, new_properties) != metadata:
            raise guard.ValidationError("comparison AVB metadata differs from the default")

        temporary_kernel = work / "Image.gz-dtb"
        temporary_kernel.write_bytes(comparison_kernel)
        guard.validate_recovery_image(
            candidate, temporary_kernel, dtbo, PARTITION_SIZE, "zstd",
            require_disabled_preinit=True,
        )
        output.mkdir(parents=True, exist_ok=True)
        with kernel_out.open("xb") as destination:
            destination.write(comparison_kernel)
        with candidate.open("rb") as source, image_out.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        print(f"8 MiB kernel: sha256={guard.sha256(comparison_kernel)}")
        print(f"8 MiB recovery: sha256={guard.sha256(image_out.read_bytes())}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--kernel", required=True, type=Path)
    parser.add_argument("--dtbo", required=True, type=Path)
    parser.add_argument("--expected-kernel-sha256", required=True)
    parser.add_argument("--expected-8m-sha256", required=True)
    parser.add_argument("--avbtool", required=True, type=Path)
    parser.add_argument("--avb-key", required=True, type=Path)
    parser.add_argument("--unpack-bootimg", required=True, type=Path)
    parser.add_argument("--mkbootimg", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    try:
        create_candidate(parser.parse_args())
    except (guard.ValidationError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"comparison candidate failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
