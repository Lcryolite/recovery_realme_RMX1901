import gzip
import importlib.util
import lzma
import struct
import subprocess
import sys
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


GZIP_INITRD_CONFIG = (
    b"CONFIG_BLK_DEV_INITRD=y\n"
    b"CONFIG_RD_GZIP=y\n"
    b"CONFIG_DECOMPRESS_GZIP=y\n"
)
ZSTD_INITRD_CONFIG = (
    b"CONFIG_RD_ZSTD=y\n"
    b"CONFIG_DECOMPRESS_ZSTD=y\n"
    b"CONFIG_ZSTD_DECOMPRESS=y\n"
)
BASE_ADB_READY_ACTION = (
    b"on property:sys.usb.config=adb && property:sys.usb.ffs.ready=1 "
    b"&& property:sys.usb.configfs=1\n"
)
DEVICE_ADB_READY_ACTION = (
    b"on property:sys.usb.ffs.ready=1 && property:sys.usb.config=adb "
    b"&& property:sys.usb.configfs=1\n"
)


def newc_entry(name: str, data: bytes, mode: int, ino: int) -> bytes:
    name_bytes = name.encode() + b"\0"
    header = b"070701" + b"".join(
        f"{value:08x}".encode()
        for value in (
            ino,
            mode,
            0,
            0,
            1,
            0,
            len(data),
            0,
            0,
            0,
            0,
            len(name_bytes),
            0,
        )
    )
    record = header + name_bytes
    record += bytes(-len(record) % 4)
    record += data
    return record + bytes(-len(record) % 4)


def fake_cpio(
    toybox: bytes,
    *,
    printf_target: bytes = b"toybox",
    dmesg_target: bytes = b"toybox",
    extra_entries: tuple[bytes, ...] = (),
) -> bytes:
    return b"".join(
        (
            newc_entry("system/bin/toybox", toybox, 0o100755, 1),
            newc_entry("system/bin/printf", printf_target, 0o120777, 2),
            newc_entry("system/bin/dmesg", dmesg_target, 0o120777, 3),
            *extra_entries,
            newc_entry("TRAILER!!!", b"", 0, 4),
        )
    )


def legacy_lz4(data: bytes) -> bytes:
    return subprocess.run(
        ["/usr/bin/lz4", "-l", "-c"],
        input=data,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout


def fake_kernel(config: bytes | None = None, image_size: int = 0x31BA000) -> bytes:
    header = bytearray(64)
    struct.pack_into("<Q", header, 8, 0x80000)
    struct.pack_into("<Q", header, 16, image_size)
    header[56:60] = b"ARMd"
    image = bytes(header) + b"kernel"
    if config is not None:
        image += b"IKCFG_ST" + gzip.compress(config, mtime=0) + b"IKCFG_ED"
    return gzip.compress(image, mtime=0) + fake_dtb(b"one") + fake_dtb(b"two")


def fake_recovery_image(
    kernel: bytes, dtbo: bytes, partition_size: int, ramdisk: bytes | None = None
) -> bytes:
    page_size = 4096
    if ramdisk is None:
        ramdisk = gzip.compress(fake_cpio(b"\x7fELF" + bytes(60)), mtime=0)
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

        self.assertEqual(gzip.decompress(gzip_image)[64:], b"kernel")
        self.assertEqual(guard.validate_dtb_group(dtb_group), [43, 43])

    def test_rejects_kernel_without_dtb(self):
        with self.assertRaisesRegex(guard.ValidationError, "no appended DTB"):
            guard.split_kernel_dtb(gzip.compress(b"kernel", mtime=0))

    def test_rejects_truncated_dtb(self):
        with self.assertRaisesRegex(guard.ValidationError, "extends|invalid DTB size"):
            guard.validate_dtb_group(fake_dtb()[:-1])

    def test_recovery_must_embed_pinned_payloads(self):
        kernel = fake_kernel(GZIP_INITRD_CONFIG)
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

    def test_recovery_can_require_disabled_bakasu_preinit(self):
        kernel = fake_kernel(GZIP_INITRD_CONFIG)
        dtbo = b"dtbo"
        partition_size = 16384
        image = fake_recovery_image(kernel, dtbo, partition_size)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "recovery.img"
            kernel_path = root / "Image.gz-dtb"
            dtbo_path = root / "dtbo.img"
            kernel_path.write_bytes(kernel)
            dtbo_path.write_bytes(dtbo)

            def set_cmdline(main: bytes, extra: bytes = b"") -> None:
                updated = bytearray(image)
                updated[64:576] = main.ljust(512, b"\0")
                updated[608:1632] = extra.ljust(1024, b"\0")
                image_path.write_bytes(updated)

            set_cmdline(b"console=ttyMSM0 kernelsu.preinit=0")
            guard.validate_recovery_image(
                image_path, kernel_path, dtbo_path, partition_size,
                require_disabled_preinit=True,
            )

            prefix = b"console=ttyMSM0 "
            suffix = b" kernelsu."
            set_cmdline(
                prefix + b"x" * (512 - len(prefix) - len(suffix)) + suffix,
                b"preinit=0",
            )
            guard.validate_recovery_image(
                image_path, kernel_path, dtbo_path, partition_size,
                require_disabled_preinit=True,
            )

            for cmdline in (
                b"console=ttyMSM0",
                b"kernelsu.preinit=1",
                b"kernelsu.preinit=0 kernelsu.preinit=1",
                b"-- kernelsu.preinit=0",
                b"kernelsu.preinit=0extra",
            ):
                with self.subTest(cmdline=cmdline):
                    set_cmdline(cmdline)
                    with self.assertRaisesRegex(
                        guard.ValidationError, "disable BakaSU preinit"
                    ):
                        guard.validate_recovery_image(
                            image_path, kernel_path, dtbo_path, partition_size,
                            require_disabled_preinit=True,
                        )

            set_cmdline(b"console=ttyMSM0")
            guard.validate_recovery_image(image_path, kernel_path, dtbo_path, partition_size)
            result = subprocess.run(
                [
                    sys.executable, str(MODULE_PATH), "image", str(image_path),
                    "--kernel", str(kernel_path), "--dtbo", str(dtbo_path),
                    "--partition-size", str(partition_size), "--require-disabled-preinit",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("disable BakaSU preinit", result.stderr)

    def test_recovery_rejects_toybox_overwritten_through_applet_symlink(self):
        kernel = fake_kernel(GZIP_INITRD_CONFIG)
        dtbo = b"dtbo"
        wrapper = (
            b"#!/system/bin/sh\n"
            b"# Atomic single-write printf wrapper for Recovery environment\n"
        )
        ramdisk = gzip.compress(fake_cpio(wrapper), mtime=0)
        partition_size = 16384

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kernel").write_bytes(kernel)
            (root / "dtbo").write_bytes(dtbo)
            (root / "recovery.img").write_bytes(
                fake_recovery_image(kernel, dtbo, partition_size, ramdisk)
            )

            with self.assertRaisesRegex(
                guard.ValidationError, "system/bin/toybox.*regular executable ELF"
            ):
                guard.validate_recovery_image(
                    root / "recovery.img",
                    root / "kernel",
                    root / "dtbo",
                    partition_size,
                )

    def test_recovery_rejects_duplicate_critical_ramdisk_path(self):
        kernel = fake_kernel(GZIP_INITRD_CONFIG)
        dtbo = b"dtbo"
        duplicate = newc_entry(
            "system/bin/toybox", b"#!/system/bin/sh\n", 0o100755, 4
        )
        ramdisk = gzip.compress(
            fake_cpio(b"\x7fELF" + bytes(60), extra_entries=(duplicate,)),
            mtime=0,
        )
        partition_size = 16384

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kernel").write_bytes(kernel)
            (root / "dtbo").write_bytes(dtbo)
            (root / "recovery.img").write_bytes(
                fake_recovery_image(kernel, dtbo, partition_size, ramdisk)
            )

            with self.assertRaisesRegex(
                guard.ValidationError,
                "duplicate newc record for system/bin/toybox",
            ):
                guard.validate_recovery_image(
                    root / "recovery.img",
                    root / "kernel",
                    root / "dtbo",
                    partition_size,
                )

    def test_recovery_requires_toybox_applet_symlinks(self):
        kernel = fake_kernel(GZIP_INITRD_CONFIG)
        dtbo = b"dtbo"
        partition_size = 16384

        for applet in ("printf", "dmesg"):
            targets = {f"{applet}_target": b"missing"}
            ramdisk = gzip.compress(
                fake_cpio(b"\x7fELF" + bytes(60), **targets), mtime=0
            )
            with (
                self.subTest(applet=applet),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                (root / "kernel").write_bytes(kernel)
                (root / "dtbo").write_bytes(dtbo)
                (root / "recovery.img").write_bytes(
                    fake_recovery_image(kernel, dtbo, partition_size, ramdisk)
                )

                with self.assertRaisesRegex(
                    guard.ValidationError,
                    f"system/bin/{applet} must be a symlink to toybox",
                ):
                    guard.validate_recovery_image(
                        root / "recovery.img",
                        root / "kernel",
                        root / "dtbo",
                        partition_size,
                    )

    def test_recovery_rejects_duplicate_adb_ready_trigger_owners(self):
        kernel = fake_kernel(GZIP_INITRD_CONFIG)
        dtbo = b"dtbo"
        init_entries = (
            newc_entry(
                "system/etc/init/hw/init.rc", BASE_ADB_READY_ACTION, 0o100644, 4
            ),
            newc_entry(
                "init.recovery.usb.rc", DEVICE_ADB_READY_ACTION, 0o100644, 5
            ),
        )
        ramdisk = gzip.compress(
            fake_cpio(b"\x7fELF" + bytes(60), extra_entries=init_entries),
            mtime=0,
        )
        partition_size = 16384

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kernel").write_bytes(kernel)
            (root / "dtbo").write_bytes(dtbo)
            (root / "recovery.img").write_bytes(
                fake_recovery_image(kernel, dtbo, partition_size, ramdisk)
            )

            with self.assertRaisesRegex(
                guard.ValidationError,
                "ADB FunctionFS ready trigger has multiple owners",
            ):
                guard.validate_recovery_image(
                    root / "recovery.img",
                    root / "kernel",
                    root / "dtbo",
                    partition_size,
                )

    def test_ramdisk_compression_must_match_kernel(self):
        ramdisk = legacy_lz4(fake_cpio(b"\x7fELF" + bytes(60)))
        for lz4_supported in (False, True):
            config = GZIP_INITRD_CONFIG
            if lz4_supported:
                config += b"CONFIG_RD_LZ4=y\nCONFIG_DECOMPRESS_LZ4=y\n"
            kernel = fake_kernel(config)
            dtbo = b"dtbo"
            image = fake_recovery_image(
                kernel, dtbo, 16384, ramdisk=ramdisk
            )
            with (
                self.subTest(lz4_supported=lz4_supported),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                image_path = root / "recovery.img"
                kernel_path = root / "Image.gz-dtb"
                dtbo_path = root / "dtbo.img"
                image_path.write_bytes(image)
                kernel_path.write_bytes(kernel)
                dtbo_path.write_bytes(dtbo)
                if lz4_supported:
                    guard.validate_recovery_image(
                        image_path, kernel_path, dtbo_path, 16384
                    )
                else:
                    with self.assertRaisesRegex(
                        guard.ValidationError, "lz4.*CONFIG_RD_LZ4"
                    ):
                        guard.validate_recovery_image(
                            image_path, kernel_path, dtbo_path, 16384
                        )

    def test_lzma_ramdisk_requires_kernel_support(self):
        ramdisk = lzma.compress(
            fake_cpio(b"\x7fELF" + bytes(60)),
            format=lzma.FORMAT_ALONE,
            preset=6,
        )
        for supported in (False, True):
            config = GZIP_INITRD_CONFIG
            if supported:
                config += b"CONFIG_RD_LZMA=y\nCONFIG_DECOMPRESS_LZMA=y\n"
            kernel = fake_kernel(config)
            with self.subTest(supported=supported), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "kernel").write_bytes(kernel)
                (root / "dtbo").write_bytes(b"dtbo")
                (root / "recovery.img").write_bytes(
                    fake_recovery_image(kernel, b"dtbo", 16384, ramdisk)
                )
                if supported:
                    guard.validate_recovery_image(
                        root / "recovery.img", root / "kernel", root / "dtbo", 16384
                    )
                else:
                    with self.assertRaisesRegex(
                        guard.ValidationError, "lzma.*CONFIG_RD_LZMA"
                    ):
                        guard.validate_recovery_image(
                            root / "recovery.img", root / "kernel", root / "dtbo", 16384
                        )

    def test_zstd_ramdisk_requires_kernel_support_and_valid_data(self):
        cpio = fake_cpio(b"\x7fELF" + bytes(60))
        ramdisk = subprocess.run(
            ["zstd", "-3", "-c", "-q"],
            input=cpio,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        large_window = subprocess.run(
            ["zstd", "-3", "--long=28", "-c", "-q"],
            input=cpio,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        cases = (
            (b"", ramdisk, "CONFIG_RD_ZSTD"),
            (b"CONFIG_RD_ZSTD=y\n", ramdisk, "CONFIG_DECOMPRESS_ZSTD"),
            (
                b"CONFIG_RD_ZSTD=y\nCONFIG_DECOMPRESS_ZSTD=y\n",
                ramdisk,
                "CONFIG_ZSTD_DECOMPRESS",
            ),
            (
                ZSTD_INITRD_CONFIG,
                ramdisk[:-2],
                "invalid zstd",
            ),
            (
                ZSTD_INITRD_CONFIG,
                large_window,
                "invalid zstd",
            ),
            (ZSTD_INITRD_CONFIG, ramdisk, None),
        )
        for extra_config, data, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                kernel = fake_kernel(GZIP_INITRD_CONFIG + extra_config)
                (root / "kernel").write_bytes(kernel)
                (root / "dtbo").write_bytes(b"dtbo")
                (root / "recovery.img").write_bytes(
                    fake_recovery_image(kernel, b"dtbo", 16384, data)
                )
                if error:
                    with self.assertRaisesRegex(guard.ValidationError, error):
                        guard.validate_recovery_image(
                            root / "recovery.img", root / "kernel", root / "dtbo", 16384
                        )
                else:
                    guard.validate_recovery_image(
                        root / "recovery.img", root / "kernel", root / "dtbo", 16384,
                        expected_ramdisk_format="zstd",
                    )

    def test_recovery_rejects_unexpected_ramdisk_format(self):
        kernel = fake_kernel(GZIP_INITRD_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kernel").write_bytes(kernel)
            (root / "dtbo").write_bytes(b"dtbo")
            (root / "recovery.img").write_bytes(
                fake_recovery_image(kernel, b"dtbo", 16384)
            )
            with self.assertRaisesRegex(
                guard.ValidationError, "format is gzip, expected zstd"
            ):
                guard.validate_recovery_image(
                    root / "recovery.img", root / "kernel", root / "dtbo", 16384,
                    expected_ramdisk_format="zstd",
                )

    def test_runtime_kernel_must_fit_abl_workspace(self):
        # The failed image's compressed ramdisk leaves exactly 0x370d000 bytes.
        ramdisk = gzip.compress(
            fake_cpio(b"\x7fELF" + bytes(60)), mtime=0
        ).ljust(29821896, b"\0")
        partition_size = 32 * 1024 * 1024
        for image_size in (0x370D000, 0x3940000):
            kernel = fake_kernel(GZIP_INITRD_CONFIG, image_size=image_size)
            with self.subTest(image_size=image_size), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "kernel").write_bytes(kernel)
                (root / "dtbo").write_bytes(b"dtbo")
                (root / "recovery.img").write_bytes(
                    fake_recovery_image(kernel, b"dtbo", partition_size, ramdisk)
                )
                if image_size == 0x370D000:
                    guard.validate_recovery_image(
                        root / "recovery.img", root / "kernel", root / "dtbo", partition_size
                    )
                else:
                    with self.assertRaisesRegex(
                        guard.ValidationError,
                        "kernel runtime size 60030976 exceeds ABL workspace 57724928",
                    ):
                        guard.validate_recovery_image(
                            root / "recovery.img", root / "kernel", root / "dtbo", partition_size
                        )


if __name__ == "__main__":
    unittest.main()
