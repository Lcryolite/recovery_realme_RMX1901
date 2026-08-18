import gzip
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "tools" / "recovery_image_guard.py"
SPEC = importlib.util.spec_from_file_location("recovery_image_guard", MODULE_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def fake_dtb(payload: bytes = b"test") -> bytes:
    total_size = 40 + len(payload)
    return guard.FDT_MAGIC + struct.pack(">I", total_size) + bytes(32) + payload


def fake_kernel() -> bytes:
    return gzip.compress(b"kernel", mtime=0) + fake_dtb(b"one") + fake_dtb(b"two")


def fake_recovery_image(kernel: bytes, dtbo: bytes, partition_size: int) -> bytes:
    page_size = 4096
    ramdisk = b"ramdisk"
    kernel_offset = page_size
    ramdisk_offset = guard.align(kernel_offset + len(kernel), page_size)
    dtbo_offset = guard.align(ramdisk_offset + len(ramdisk), page_size)
    image = bytearray(partition_size)
    image[:8] = guard.BOOT_MAGIC
    struct.pack_into("<I", image, 8, len(kernel))
    struct.pack_into("<I", image, 16, len(ramdisk))
    struct.pack_into("<I", image, 36, page_size)
    struct.pack_into("<I", image, 40, 1)
    struct.pack_into("<I", image, 1632, len(dtbo))
    struct.pack_into("<Q", image, 1636, dtbo_offset)
    struct.pack_into("<I", image, 1644, guard.BOOT_HEADER_V1_SIZE)
    image[kernel_offset : kernel_offset + len(kernel)] = kernel
    image[ramdisk_offset : ramdisk_offset + len(ramdisk)] = ramdisk
    image[dtbo_offset : dtbo_offset + len(dtbo)] = dtbo
    image[-64:-60] = guard.AVB_FOOTER_MAGIC
    return bytes(image)


class RecoveryImageGuardTest(unittest.TestCase):
    def test_valid_kernel_and_dtb_group(self):
        kernel = fake_kernel()
        gzip_image, dtb_group = guard.split_kernel_dtb(kernel)

        self.assertEqual(gzip.decompress(gzip_image), b"kernel")
        self.assertEqual(guard.validate_dtb_group(dtb_group), [43, 43])

    def test_rejects_kernel_without_dtb(self):
        with self.assertRaisesRegex(guard.ValidationError, "no appended DTB"):
            guard.split_kernel_dtb(gzip.compress(b"kernel", mtime=0))

    def test_rejects_truncated_dtb(self):
        with self.assertRaisesRegex(guard.ValidationError, "extends|invalid DTB size"):
            guard.validate_dtb_group(fake_dtb()[:-1])

    def test_recovery_must_embed_pinned_payloads(self):
        kernel = fake_kernel()
        dtbo = b"dtbo"
        partition_size = 16384
        image = fake_recovery_image(kernel, dtbo, partition_size)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "recovery.img"
            kernel_path = root / "Image.gz-dtb"
            dtbo_path = root / "dtbo.img"
            image_path.write_bytes(image)
            kernel_path.write_bytes(kernel)
            dtbo_path.write_bytes(dtbo)

            guard.validate_recovery_image(
                image_path, kernel_path, dtbo_path, partition_size
            )

            changed_kernel = bytearray(kernel)
            changed_kernel[10] ^= 1
            kernel_path.write_bytes(changed_kernel)
            with self.assertRaisesRegex(guard.ValidationError, "pinned kernel"):
                guard.validate_recovery_image(
                    image_path, kernel_path, dtbo_path, partition_size
                )


if __name__ == "__main__":
    unittest.main()
