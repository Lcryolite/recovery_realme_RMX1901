import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).parents[1]
SRC = ROOT / "recovery/diagnostics"


class RecoveryDiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("cc"):
            raise unittest.SkipTest("C compiler unavailable")
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "diagnostics"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-DRMX_PSTORE_EMBEDDED", str(SRC / "diagnostics.c"),
                        str(SRC / "pstore_snapshot.c"), "-o", str(cls.binary)],
                       capture_output=True, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_recreates_missing_cache_log_directory_after_wipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "recovery", "kmsg", "status"))
            source.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            harness = root / "ensure-cache.c"
            helper = root / "ensure-cache"
            harness.write_text('#define main diagnostics_main\n'
                               f'#include "{SRC / "diagnostics.c"}"\n'
                               '#undef main\n'
                               'int main(int argc, char **argv) {\n'
                               '    return argc == 2 && !ensure_cache_recovery(argv[1]) ? 0 : 1;\n'
                               '}\n')
            subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-DRMX_PSTORE_EMBEDDED", str(harness), str(SRC / "pstore_snapshot.c"),
                            "-o", str(helper)], capture_output=True, check=True)
            self.assertEqual(subprocess.run([str(helper), str(cache)]).returncode, 0)
            result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                     "boot-001", str(kernel), str(status)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"kernel event", (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes())
            self.assertIn("state=ok", status.read_text())

    def test_refuses_symlink_as_cache_log_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, target, kernel, status = (root / name for name in
                                                      ("pstore", "recovery", "target", "kmsg", "status"))
            source.mkdir()
            target.mkdir()
            cache.symlink_to(target, target_is_directory=True)
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                     "boot-001", str(kernel), str(status)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(target.iterdir()), [])

    def test_empty_pstore_record_is_reported_as_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            (source / "dmesg-ramoops-0").write_bytes(b"")
            (source / "console-ramoops-0").write_bytes(b"console evidence")
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                     "boot-001", str(kernel), str(status)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            session = cache / "rmx1901-diagnostics/current/pstore"
            records = json.loads((session / "records.json").read_text())
            self.assertEqual(records["status"], "partial")
            self.assertEqual((session / "pstore-console-ramoops-0.raw").read_bytes(), b"console evidence")
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=pstore_empty_record", status.read_text())

    def test_archives_raw_pstore_and_kernel_without_userspace_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            (source / "console-ramoops-0").write_bytes(b"prior console\x00data")
            kernel.write_bytes(b"6,1,100,-;kernel event\n7,2,200,-;kernel debug event\n"
                               b"14,3,300,-;userspace event\n")
            result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                     "boot-001", str(kernel), str(status)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            session = cache / "rmx1901-diagnostics/current"
            record = json.loads((session / "pstore/records.json").read_text())["records"][0]
            self.assertEqual((session / "pstore" / record["file"]).read_bytes(), b"prior console\x00data")
            saved = (session / "kernel.current").read_bytes()
            self.assertIn(b"kernel event", saved)
            self.assertNotIn(b"kernel debug event", saved)
            self.assertNotIn(b"userspace event", saved)
            self.assertIn("state=ok", status.read_text())
            self.assertEqual((source / "console-ramoops-0").read_bytes(), b"prior console\x00data")

    def test_archives_device_uboot_records_without_changing_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status, logfs = (root / name for name in
                                                     ("pstore", "cache", "kmsg", "status", "logfs"))
            source.mkdir()
            cache.mkdir()
            logfs.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            original = {}
            for slot in (1, 2):
                nonce = f"usb-attempt-{slot}"
                record = (f"RMX1901_UBOOT_LOG v1\ncandidate={nonce}\nconsole_overflow=0\n\n"
                          f"RMX1901_USB_CONSOLE attempt={nonce}\nmessage-{slot}\n").encode()
                path = logfs / f"RMXLOG{slot:02d}.LOG"
                path.write_bytes(record)
                original[slot] = record
            result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                     "boot-001", str(kernel), str(status), str(logfs)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            session = cache / "rmx1901-diagnostics/current"
            for slot, record in original.items():
                self.assertEqual((logfs / f"RMXLOG{slot:02d}.LOG").read_bytes(), record)
                self.assertEqual((session / f"uboot-logfs-{slot:02d}.log").read_bytes(), record)
                metadata = json.loads((session / f"uboot-logfs-{slot:02d}.json").read_text())
                self.assertEqual(metadata["source"], "uboot_logfs")
                self.assertEqual(metadata["candidate_nonce"], f"usb-attempt-{slot}")
                self.assertFalse(metadata["boot_instance_verified"])
                self.assertFalse(metadata["console_overflow"])
            self.assertIn("state=ok", status.read_text())

    def test_kernel_device_metadata_does_not_stop_log_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;device probe\n SUBSYSTEM=platform\n DEVICE=+platform:test\n\n"
                               b"7,2,200,-;debug probe\n SUBSYSTEM=debug\n\n"
                               b"6,3,300,-;later event\n")
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            saved = (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes()
            self.assertIn(b"device probe\n SUBSYSTEM=platform\n DEVICE=+platform:test\n", saved)
            self.assertIn(b"later event", saved)
            self.assertNotIn(b"debug probe", saved)
            self.assertNotIn(b"SUBSYSTEM=debug", saved)
            self.assertIn("state=ok", status.read_text())

    def test_restarting_same_boot_archives_only_new_kernel_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            old = b"6,1,100,-;old event\n SUBSYSTEM=platform\n7,2,200,-;debug event\n"
            new = b"6,3,300,-;new event\n DEVICE=+platform:test\n"
            kernel.write_bytes(old)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            kernel.write_bytes(old + new)
            for _ in range(2):
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            saved = (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes()
            self.assertEqual(saved.count(b"old event"), 1)
            self.assertEqual(saved.count(b"new event"), 1)
            self.assertEqual(saved.count(b"SUBSYSTEM=platform"), 1)
            self.assertEqual(saved.count(b"DEVICE=+platform:test"), 1)
            self.assertNotIn(b"debug event", saved)
            self.assertIn("state=ok", status.read_text())

    def test_restart_reports_a_gap_in_the_available_kernel_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;old event\n")
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            kernel.write_bytes(b"6,4,400,-;new event\n")
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=kernel_truncated", status.read_text())
            saved = (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes()
            self.assertIn(b"old event", saved)
            self.assertIn(b"new event", saved)

    def test_restart_replays_record_that_was_not_fully_checkpointed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            old = b"6,0,100,-;complete event\n"
            header = b"6,1,200,-;device probe\n"
            kernel.write_bytes(old)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            saved = cache / "rmx1901-diagnostics/current/kernel.current"
            with saved.open("ab") as stream:
                stream.write(header)
            kernel.write_bytes(old + header + b" SUBSYSTEM=platform\n")
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn(header + b" SUBSYSTEM=platform\n", saved.read_bytes())
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=kernel_truncated", status.read_text())

    def test_stopping_stream_flushes_current_kernel_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            os.mkfifo(kernel)
            fifo = os.open(kernel, os.O_RDWR | os.O_NONBLOCK)
            process = subprocess.Popen([str(self.binary), "--stream", str(source), str(cache),
                                        "boot-001", str(kernel), str(status)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            try:
                saved = cache / "rmx1901-diagnostics/current/kernel.current"
                deadline = time.monotonic() + 5
                while not saved.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(saved.exists())
                os.write(fifo, b"6,1,100,-;flush before stop\n SUBSYSTEM=platform\n")
                while b"SUBSYSTEM=platform" not in saved.read_bytes() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertIn(b"SUBSYSTEM=platform", saved.read_bytes())
                process.terminate()
                process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertIn("state=ok", status.read_text())
            finally:
                os.close(fifo)
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_debug_kernel_messages_are_archived_only_with_explicit_private_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status, logfs, debug = (root / name for name in
                                                           ("pstore", "cache", "kmsg", "status", "logfs", "debug"))
            source.mkdir()
            cache.mkdir()
            logfs.mkdir()
            kernel.write_bytes(b"6,1,100,-;info event\n7,2,200,-;debug event\n")
            debug.write_text("1\n")
            debug.chmod(0o600)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001",
                       str(kernel), str(status), str(logfs), str(debug)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            saved = (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes()
            self.assertIn(b"info event", saved)
            self.assertIn(b"debug event", saved)
            debug.chmod(0o644)
            command[4] = "boot-002"
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            saved = (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes()
            self.assertIn(b"info event", saved)
            self.assertNotIn(b"debug event", saved)

    def test_invalid_or_truncated_device_uboot_record_is_reported_as_partial(self):
        for record in (b"not a U-Boot record", b"RMX1901_UBOOT_LOG v1\ncandidate=usb-overflow\n"
                       b"console_overflow=1\n\nRMX1901_USB_CONSOLE attempt=usb-overflow\n"):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, cache, kernel, status, logfs = (root / name for name in
                                                         ("pstore", "cache", "kmsg", "status", "logfs"))
                source.mkdir()
                cache.mkdir()
                logfs.mkdir()
                kernel.write_bytes(b"6,1,100,-;kernel event\n")
                (logfs / "RMXLOG01.LOG").write_bytes(record)
                result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                         "boot-001", str(kernel), str(status), str(logfs)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("state=partial", status.read_text())
                self.assertIn("reason=uboot_logfs_incomplete", status.read_text())
                self.assertEqual((logfs / "RMXLOG01.LOG").read_bytes(), record)
                self.assertIn(b"kernel event", (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes())

    def test_archives_early_uboot_record_before_usb_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status, logfs = (root / name for name in
                                                     ("pstore", "cache", "kmsg", "status", "logfs"))
            source.mkdir()
            cache.mkdir()
            logfs.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            record = (b"RMX1901_UBOOT_LOG v1\ncandidate=uboot-early-1\n"
                      b"console_overflow=0\n\nearly firmware event\n")
            (logfs / "RMXLOG01.LOG").write_bytes(record)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001",
                       str(kernel), str(status), str(logfs)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            session = cache / "rmx1901-diagnostics/current"
            self.assertEqual((session / "uboot-logfs-01.log").read_bytes(), record)
            self.assertEqual(json.loads((session / "uboot-logfs-01.json").read_text())
                             ["candidate_nonce"], "uboot-early-1")
            self.assertIn("state=ok", status.read_text())

    def test_truncated_uboot_archive_is_reported_even_when_metadata_exists(self):
        for kind in ("logfs", "usb"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, cache, kernel, status, logfs, usb = (root / name for name in
                                                             ("pstore", "cache", "kmsg", "status", "logfs", "usb"))
                source.mkdir()
                cache.mkdir()
                logfs.mkdir()
                kernel.write_bytes(b"6,1,100,-;kernel event\n")
                record = (b"RMX1901_UBOOT_LOG v1\ncandidate=uboot-1\n"
                          b"console_overflow=0\n\nearly firmware event\n")
                (logfs / "RMXLOG01.LOG").write_bytes(record)
                command = [str(self.binary), "--once", str(source), str(cache), "boot-001",
                           str(kernel), str(status), str(logfs)]
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                session = cache / "rmx1901-diagnostics/current"
                if kind == "logfs":
                    saved = session / "uboot-logfs-01.log"
                    reason = "uboot_logfs_incomplete"
                else:
                    usb.write_bytes(b"complete usb transcript")
                    usb.chmod(0o600)
                    digest = hashlib.sha256(usb.read_bytes()).hexdigest()
                    imported = [str(self.binary), "--import-uboot", str(cache), str(usb),
                                "usb-attempt", digest, "boot-001", str(status)]
                    self.assertEqual(subprocess.run(imported, capture_output=True).returncode, 0)
                    saved = session / "uboot-usb.log"
                    reason = "uboot_incomplete"
                saved.write_bytes(saved.read_bytes()[:7])
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                self.assertIn("state=partial", status.read_text())
                self.assertIn("reason=" + reason, status.read_text())
                self.assertEqual((logfs / "RMXLOG01.LOG").read_bytes(), record)

    def test_changed_device_uboot_archive_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status, logfs = (root / name for name in
                                                     ("pstore", "cache", "kmsg", "status", "logfs"))
            source.mkdir()
            cache.mkdir()
            logfs.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            record = (b"RMX1901_UBOOT_LOG v1\ncandidate=uboot-1\n"
                      b"console_overflow=0\n\nearly firmware event\n")
            (logfs / "RMXLOG01.LOG").write_bytes(record)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001",
                       str(kernel), str(status), str(logfs)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            saved = cache / "rmx1901-diagnostics/current/uboot-logfs-01.log"
            saved.write_bytes(b"X" + saved.read_bytes()[1:])
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("state=partial", status.read_text())
            self.assertEqual((logfs / "RMXLOG01.LOG").read_bytes(), record)

    def test_incomplete_uboot_metadata_is_reported_as_partial(self):
        for kind in ("logfs", "usb"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, cache, kernel, status, logfs, usb = (root / name for name in
                                                             ("pstore", "cache", "kmsg", "status", "logfs", "usb"))
                source.mkdir()
                cache.mkdir()
                logfs.mkdir()
                kernel.write_bytes(b"6,1,100,-;kernel event\n")
                (logfs / "RMXLOG01.LOG").write_bytes(
                    b"RMX1901_UBOOT_LOG v1\ncandidate=uboot-1\nconsole_overflow=0\n\nearly event\n")
                command = [str(self.binary), "--once", str(source), str(cache), "boot-001",
                           str(kernel), str(status), str(logfs)]
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                session = cache / "rmx1901-diagnostics/current"
                if kind == "logfs":
                    metadata = session / "uboot-logfs-01.json"
                else:
                    usb.write_bytes(b"usb event")
                    usb.chmod(0o600)
                    imported = [str(self.binary), "--import-uboot", str(cache), str(usb), "usb-attempt",
                                hashlib.sha256(usb.read_bytes()).hexdigest(), "boot-001", str(status)]
                    self.assertEqual(subprocess.run(imported, capture_output=True).returncode, 0)
                    metadata = session / "uboot-usb.json"
                metadata.write_bytes(metadata.read_bytes()[:-2])
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                self.assertIn("state=partial", status.read_text())

    def test_missing_or_short_pstore_record_is_reported_as_partial(self):
        for mutation in ("short", "missing", "manifest"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
                source.mkdir()
                cache.mkdir()
                kernel.write_bytes(b"6,1,100,-;kernel event\n")
                (source / "dmesg-ramoops-0").write_bytes(b"crash record with evidence")
                command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                pstore = cache / "rmx1901-diagnostics/current/pstore"
                record = pstore / "pstore-dmesg-ramoops-0.raw"
                if mutation == "short":
                    record.write_bytes(b"bad")
                elif mutation == "missing":
                    record.unlink()
                else:
                    metadata = pstore / "records.json"
                    metadata.write_bytes(metadata.read_bytes()[:-2])
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                self.assertIn("state=partial", status.read_text())
                self.assertIn("reason=pstore_incomplete", status.read_text())
                self.assertEqual((source / "dmesg-ramoops-0").read_bytes(), b"crash record with evidence")

    def test_missing_logfs_device_does_not_prevent_kernel_archival(self):
        if Path("/dev/block/bootdevice/by-name/logfs").exists():
            self.skipTest("requires a host without the RMX1901 logfs device")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            command = [str(self.binary), "--device-once", str(source), str(cache),
                       "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=uboot_logfs_incomplete", status.read_text())
            self.assertIn(b"kernel event", (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes())

    def test_stream_retries_an_unavailable_logfs_and_clears_the_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status, logfs = (root / name for name in
                                                     ("pstore", "cache", "kmsg", "status", "logfs"))
            source.mkdir()
            cache.mkdir()
            os.mkfifo(kernel)
            writer = os.open(kernel, os.O_RDWR | os.O_NONBLOCK)
            process = subprocess.Popen([str(self.binary), "--stream", str(source), str(cache),
                                        "boot-001", str(kernel), str(status), str(logfs)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while (not status.exists() or "state=partial" not in status.read_text()) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIsNone(process.poll())
                self.assertIn("state=partial", status.read_text())
                logfs.mkdir()
                record = (b"RMX1901_UBOOT_LOG v1\ncandidate=uboot-retry\n"
                          b"console_overflow=0\n\nearly event\n")
                (logfs / "RMXLOG01.LOG").write_bytes(record)
                saved = cache / "rmx1901-diagnostics/current/uboot-logfs-01.log"
                deadline = time.monotonic() + 9
                while (not saved.exists() or "state=ok" not in status.read_text()) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIsNone(process.poll())
                self.assertEqual(saved.read_bytes(), record)
                self.assertIn("state=ok", status.read_text())
            finally:
                process.terminate()
                process.wait(timeout=5)
                os.close(writer)

    def test_full_sessions_with_device_logs_rotate_below_the_total_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status, logfs, usb = (root / name for name in
                                                         ("pstore", "cache", "kmsg", "status", "logfs", "usb"))
            source.mkdir()
            cache.mkdir()
            logfs.mkdir()
            (source / "console-ramoops-0").write_bytes(b"p" * (4 * 1024 * 1024))
            kernel.write_bytes((b"6,1,1,-;" + b"k" * 900 + b"\n") * 1500)
            usb.write_bytes(b"u" * (256 * 1024))
            usb.chmod(0o600)
            digest = hashlib.sha256(usb.read_bytes()).hexdigest()
            for slot in (1, 2, 3):
                header = (f"RMX1901_UBOOT_LOG v1\ncandidate=slot-{slot}\n"
                          "console_overflow=0\n\n").encode()
                (logfs / f"RMXLOG{slot:02d}.LOG").write_bytes(header + b"l" * (32768 - len(header)))
            for boot in range(1, 5):
                boot_id = f"boot-{boot:03d}"
                command = [str(self.binary), "--once", str(source), str(cache), boot_id,
                           str(kernel), str(status), str(logfs)]
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                command = [str(self.binary), "--import-uboot", str(cache), str(usb),
                           "usb-attempt", digest, boot_id, str(status)]
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            base = cache / "rmx1901-diagnostics"
            self.assertEqual([(base / name / "boot-id").read_text().strip()
                              for name in ("current", "previous", "oldest")],
                             ["boot-004", "boot-003", "boot-002"])
            self.assertLessEqual(sum(p.stat().st_size for p in base.rglob("*") if p.is_file()),
                                 16 * 1024 * 1024)
            for slot in (1, 2, 3):
                self.assertEqual((base / f"current/uboot-logfs-{slot:02d}.log").read_bytes(),
                                 (logfs / f"RMXLOG{slot:02d}.LOG").read_bytes())
            (source / "console-ramoops-0").unlink()
            (source / "console-ramoops-0").symlink_to(root / "not-a-record")
            (source / "dmesg-ramoops-0").write_bytes(b"p" * (3 * 1024 * 1024))
            command = [str(self.binary), "--once", str(source), str(cache), "boot-005",
                       str(kernel), str(status), str(logfs)]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((base / "current/boot-id").read_text().strip(), "boot-004")
            self.assertEqual((base / "pending/pstore/pstore-dmesg-ramoops-0.raw").stat().st_size,
                             3 * 1024 * 1024)
            self.assertLessEqual(sum(p.stat().st_size for p in base.rglob("*") if p.is_file()),
                                 16 * 1024 * 1024)

    def test_keeps_three_newest_boot_sessions_and_unrelated_cache_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            (cache / "unrelated").write_bytes(b"keep me")
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            for number in range(1, 5):
                result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                         f"boot-{number:03d}", str(kernel), str(status)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            base = cache / "rmx1901-diagnostics"
            self.assertEqual((base / "current/boot-id").read_text().strip(), "boot-004")
            self.assertEqual((base / "previous/boot-id").read_text().strip(), "boot-003")
            self.assertEqual((base / "oldest/boot-id").read_text().strip(), "boot-002")
            self.assertEqual((cache / "unrelated").read_bytes(), b"keep me")

    def test_kernel_stream_retains_a_bounded_tail_and_marks_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            lines = [f"6,{i},1,-;event-{i:06d}-".encode() + b"x" * 90 + b"\n" for i in range(15000)]
            kernel.write_bytes(b"".join(lines))
            result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                     "boot-001", str(kernel), str(status)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            session = cache / "rmx1901-diagnostics/current"
            self.assertTrue((session / "kernel.truncated").is_file())
            files = [session / "kernel.previous", session / "kernel.current"]
            self.assertLessEqual(sum(path.stat().st_size for path in files), 1024 * 1024)
            self.assertTrue(files[-1].read_bytes().endswith(lines[-1]))
            self.assertIn("state=partial", status.read_text())

    def test_failed_snapshot_does_not_block_retry_for_the_same_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            command = [str(self.binary), "--once", str(source), str(cache),
                       "boot-001", str(kernel), str(status)]
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("state=error", status.read_text())
            source.mkdir()
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("state=ok", status.read_text())

    def test_failed_new_snapshot_does_not_rotate_existing_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            for boot in range(1, 4):
                command[4] = f"boot-{boot:03d}"
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            source.rmdir()
            command[4] = "boot-004"
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            base = cache / "rmx1901-diagnostics"
            self.assertEqual([(base / name / "boot-id").read_text().strip()
                              for name in ("current", "previous", "oldest")],
                             ["boot-003", "boot-002", "boot-001"])
            self.assertIn("state=error", status.read_text())

    def test_recovers_interrupted_pstore_copy_without_losing_its_only_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;new kernel event\n")
            pending = cache / "rmx1901-diagnostics/pending/pstore"
            pending.mkdir(parents=True, mode=0o700)
            (cache / "rmx1901-diagnostics").chmod(0o700)
            pending.parent.chmod(0o700)
            record = pending / "pstore-dmesg-ramoops-0.raw"
            record.write_bytes(b"only surviving crash record")
            record.chmod(0o600)
            (pending / "records.json").write_bytes(b"")
            (pending / "records.json").chmod(0o600)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            base = cache / "rmx1901-diagnostics"
            self.assertEqual((base / "previous/pstore/pstore-dmesg-ramoops-0.raw").read_bytes(),
                             b"only surviving crash record")
            self.assertTrue((base / "previous/incomplete").is_file())
            self.assertIn(b"new kernel event", (base / "current/kernel.current").read_bytes())
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=pstore_interrupted", status.read_text())
            command[4] = "boot-002"
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((base / "oldest/pstore/pstore-dmesg-ramoops-0.raw").read_bytes(),
                             b"only surviving crash record")
            command[4] = "boot-003"
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual([(base / name / "boot-id").read_text().strip()
                              for name in ("current", "previous", "oldest")],
                             ["boot-003", "boot-002", "boot-001"])

    def test_empty_pending_metadata_does_not_replace_an_old_session(self):
        for case in ("empty_manifest", "boot_id", "empty_record", "empty_record_manifest"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
                source.mkdir()
                cache.mkdir()
                kernel.write_bytes(b"6,1,100,-;kernel event\n")
                command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
                for boot in range(1, 4):
                    command[4] = f"boot-{boot:03d}"
                    self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                base = cache / "rmx1901-diagnostics"
                pending = base / "pending"
                pending.mkdir(mode=0o700)
                (pending / "pstore").mkdir(mode=0o700)
                if case == "empty_manifest":
                    metadata = pending / "pstore/records.json"
                    metadata.write_text('{"status":"empty","records":[]}\n')
                    metadata.chmod(0o600)
                elif case == "boot_id":
                    marker = pending / "boot-id"
                    marker.write_text("boot-old\n")
                    marker.chmod(0o600)
                elif case == "empty_record":
                    record = pending / "pstore/pstore-dmesg-ramoops-0.raw"
                    record.write_bytes(b"")
                    record.chmod(0o600)
                else:
                    manifest = pending / "pstore/records.json"
                    manifest.write_text('{"status":"partial","records":[{"source_name":"dmesg-ramoops-0",'
                                        '"type":"crash","origin":"unknown","file":"",'
                                        '"bytes":0,"saved_bytes":0,"truncated":false}]}\n')
                    manifest.chmod(0o600)
                command[4] = "boot-004"
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
                self.assertEqual([(base / name / "boot-id").read_text().strip()
                                  for name in ("current", "previous", "oldest")],
                                 ["boot-004", "boot-003", "boot-002"])
                if case in ("empty_record", "empty_record_manifest"):
                    self.assertIn("reason=pstore_interrupted", status.read_text())
                else:
                    self.assertIn("state=ok", status.read_text())

    def test_interrupted_pstore_recovery_can_resume_after_another_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            pending = cache / "rmx1901-diagnostics/pending"
            (pending / "pstore").mkdir(parents=True, mode=0o700)
            pending.parent.chmod(0o700)
            pending.chmod(0o700)
            for name, data in {"boot-id": b"interrupted-boot-old\n", "boot-id.interrupted": b"boot-earlier\n",
                               "incomplete": b"unfinished\n", "pstore/pstore-dmesg-ramoops-0.raw": b"crash"}.items():
                path = pending / name
                path.write_bytes(data)
                path.chmod(0o600)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-new", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            previous = cache / "rmx1901-diagnostics/previous"
            self.assertEqual((previous / "boot-id").read_text().strip(), "interrupted-boot-old")
            self.assertEqual((previous / "boot-id.interrupted").read_text().strip(), "boot-earlier")
            self.assertEqual((previous / "pstore/pstore-dmesg-ramoops-0.raw").read_bytes(), b"crash")
            self.assertIn("reason=pstore_interrupted", status.read_text())

    def test_empty_pending_notice_survives_a_failed_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            for boot in range(1, 4):
                command[4] = f"boot-{boot:03d}"
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            base = cache / "rmx1901-diagnostics"
            pending = base / "pending/pstore"
            pending.mkdir(parents=True, mode=0o700)
            pending.parent.chmod(0o700)
            record = pending / "pstore-dmesg-ramoops-0.raw"
            record.write_bytes(b"")
            record.chmod(0o600)
            failure = root / "fail-mkdir.c"
            failure.write_text(r'''
#define _GNU_SOURCE
#include <errno.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <unistd.h>
int mkdirat(int fd, const char *path, mode_t mode) {
    if (!strcmp(path, "pending")) { errno = ENOSPC; return -1; }
    return syscall(SYS_mkdirat, fd, path, mode);
}
''')
            preload = root / "fail-mkdir.so"
            subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                            str(failure), "-o", str(preload)], check=True, capture_output=True)
            command[4] = "boot-004"
            env = dict(os.environ, LD_PRELOAD=str(preload))
            self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=pstore_interrupted", status.read_text())
            self.assertEqual([(base / name / "boot-id").read_text().strip()
                              for name in ("current", "previous", "oldest")],
                             ["boot-004", "boot-003", "boot-002"])

    def test_failed_warning_sync_does_not_delete_empty_pending_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            base = cache / "rmx1901-diagnostics"
            pending = base / "pending/pstore"
            pending.mkdir(parents=True, mode=0o700)
            base.chmod(0o700)
            pending.parent.chmod(0o700)
            record = pending / "pstore-dmesg-ramoops-0.raw"
            record.write_bytes(b"")
            record.chmod(0o600)
            failure = root / "fail-warning.c"
            failure.write_text(r'''
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>
int fsync(int fd) {
    char link[64], path[4096];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof(path) - 1);
    if (n > 0) {
        path[n] = 0;
        if (strstr(path, "/pstore.pending-warning")) { errno = EIO; return -1; }
    }
    return syscall(SYS_fsync, fd);
}
''')
            preload = root / "fail-warning.so"
            subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                            str(failure), "-o", str(preload)], check=True, capture_output=True)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            env = dict(os.environ, LD_PRELOAD=str(preload))
            for _ in range(2):
                self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
                self.assertTrue(record.exists())
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("reason=pstore_interrupted", status.read_text())

    def test_same_boot_keeps_distinct_interrupted_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            base = cache / "rmx1901-diagnostics"
            current, pending = base / "current", base / "pending"
            for path in (current / "pstore", pending / "pstore"):
                path.mkdir(parents=True, mode=0o700)
                path.parent.chmod(0o700)
            base.chmod(0o700)
            for path, data in ((current / "boot-id", b"interrupted-boot-new\n"),
                               (current / "incomplete", b"unfinished\n"),
                               (current / "pstore/pstore-dmesg-ramoops-0.raw", b"first crash"),
                               (pending / "pstore/pstore-dmesg-ramoops-0.raw", b"second crash")):
                path.write_bytes(data)
                path.chmod(0o600)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-new", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((base / "previous/pstore/pstore-dmesg-ramoops-0.raw").read_bytes(), b"second crash")
            self.assertEqual((base / "oldest/pstore/pstore-dmesg-ramoops-0.raw").read_bytes(), b"first crash")
            self.assertIn("reason=pstore_interrupted", status.read_text())

    def test_incomplete_current_session_is_reported_after_another_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            current = cache / "rmx1901-diagnostics/current"
            (current / "pstore").mkdir(parents=True, mode=0o700)
            current.parent.chmod(0o700)
            current.chmod(0o700)
            for name, data in {"boot-id": b"interrupted-boot-old\n", "incomplete": b"unfinished\n",
                               "pstore/pstore-dmesg-ramoops-0.raw": b"crash"}.items():
                path = current / name
                path.write_bytes(data)
                path.chmod(0o600)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-new", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((cache / "rmx1901-diagnostics/previous/pstore/pstore-dmesg-ramoops-0.raw").read_bytes(), b"crash")
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=pstore_interrupted", status.read_text())

    def test_missing_cache_reports_an_error_without_creating_an_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                     "boot-001", str(kernel), str(status)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("state=error", status.read_text())
            self.assertFalse(cache.exists())

    def test_kernel_sync_failure_is_reported_instead_of_claiming_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            (source / "console-ramoops-0").write_bytes(b"preserve original")
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            failure = root / "fail.c"
            failure.write_text(r'''
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>
int fsync(int fd) {
    char link[64], path[4096];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof(path) - 1);
    if (n > 0) {
        path[n] = 0;
        if (strstr(path, "/kernel.current")) { errno = ENOSPC; return -1; }
    }
    return syscall(SYS_fsync, fd);
}
''')
            preload = root / "fail.so"
            subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                            str(failure), "-o", str(preload)], check=True, capture_output=True)
            env = dict(os.environ, LD_PRELOAD=str(preload))
            result = subprocess.run([str(self.binary), "--once", str(source), str(cache),
                                     "boot-001", str(kernel), str(status)], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("state=error", status.read_text())
            self.assertEqual((source / "console-ramoops-0").read_bytes(), b"preserve original")

    def test_interrupted_kernel_write_stays_marked_and_does_not_merge_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            failure = root / "fail.c"
            failure.write_text(r'''
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>
ssize_t write(int fd, const void *data, size_t count) {
    static int phase;
    char link[64], path[4096];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof(path) - 1);
    if (n > 0) {
        path[n] = 0;
        if (strstr(path, "/kernel.current")) {
            if (!phase++) return syscall(SYS_write, fd, data, count > 5 ? 5 : count);
            errno = EIO; return -1;
        }
    }
    return syscall(SYS_write, fd, data, count);
}
''')
            preload = root / "fail.so"
            subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                            str(failure), "-o", str(preload)], check=True, capture_output=True)
            command = [str(self.binary), "--once", str(source), str(cache), "boot-001", str(kernel), str(status)]
            env = dict(os.environ, LD_PRELOAD=str(preload))
            self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
            self.assertIn("state=error", status.read_text())
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            data = (cache / "rmx1901-diagnostics/current/kernel.current").read_bytes()
            self.assertIn(b"\n6,1,100,-;kernel event\n", data)
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=kernel_truncated", status.read_text())

    def test_rotation_refuses_to_remove_an_unknown_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            for number in range(1, 4):
                command = [str(self.binary), "--once", str(source), str(cache),
                           f"boot-{number:03d}", str(kernel), str(status)]
                self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            base = cache / "rmx1901-diagnostics"
            unknown = base / "oldest" / "do-not-delete"
            unknown.write_text("keep")
            command[4] = "boot-004"
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(unknown.read_text(), "keep")
            self.assertEqual([(base / name / "boot-id").read_text().strip()
                              for name in ("current", "previous", "oldest")],
                             ["boot-003", "boot-002", "boot-001"])
            self.assertIn("state=error", status.read_text())

    def test_incomplete_existing_session_is_not_treated_as_a_valid_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            command = [str(self.binary), "--once", str(source), str(cache),
                       "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            session = cache / "rmx1901-diagnostics/current"
            shutil.rmtree(session / "pstore")
            command[4] = "boot-002"
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((session / "boot-id").read_text().strip(), "boot-001")
            self.assertIn("state=error", status.read_text())

    def test_imports_host_usb_log_with_provenance_and_keeps_its_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status, transcript = (root / name for name in
                                                         ("pstore", "cache", "kmsg", "status", "usb.log"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            command = [str(self.binary), "--once", str(source), str(cache),
                       "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            data = b"old" * 100000 + b"newest usb failure"
            transcript.write_bytes(data)
            transcript.chmod(0o600)
            digest = hashlib.sha256(data).hexdigest()
            command = [str(self.binary), "--import-uboot", str(cache), str(transcript),
                       "usb-attempt-1", digest, "boot-001", str(status)]
            wrong_boot = command.copy()
            wrong_boot[-2] = "boot-002"
            self.assertNotEqual(subprocess.run(wrong_boot, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            session = cache / "rmx1901-diagnostics/current"
            saved = (session / "uboot-usb.log").read_bytes()
            metadata = json.loads((session / "uboot-usb.json").read_text())
            self.assertEqual(saved, data[-128 * 1024:])
            self.assertEqual(metadata["source"], "uboot_usb_console")
            self.assertEqual(metadata["candidate_nonce"], "usb-attempt-1")
            self.assertEqual(metadata["reported_sha256"], digest)
            self.assertFalse(metadata["boot_instance_verified"])
            self.assertTrue(metadata["truncated"])
            self.assertIn("state=partial", status.read_text())
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual((session / "uboot-usb.log").read_bytes(), saved)

    def test_an_interrupted_usb_import_is_reported_as_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            command = [str(self.binary), "--once", str(source), str(cache),
                       "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            orphan = cache / "rmx1901-diagnostics/current/uboot-usb.log"
            orphan.write_bytes(b"partial transcript")
            orphan.chmod(0o600)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=uboot_incomplete", status.read_text())
            self.assertEqual(orphan.read_bytes(), b"partial transcript")

    def test_truncated_usb_metadata_is_reported_even_if_marker_write_was_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            kernel.write_bytes(b"6,1,100,-;kernel event\n")
            command = [str(self.binary), "--once", str(source), str(cache),
                       "boot-001", str(kernel), str(status)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            session = cache / "rmx1901-diagnostics/current"
            (session / "uboot-usb.log").write_bytes(b"tail")
            (session / "uboot-usb.log").chmod(0o600)
            (session / "uboot-usb.json").write_text(
                '{"source":"uboot_usb_console","candidate_nonce":"usb-test","reported_sha256":"' +
                "0" * 64 + '","boot_instance_verified":false,"source_bytes":5,"stored_bytes":4,"truncated":true}\n')
            (session / "uboot-usb.json").chmod(0o600)
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertIn("state=partial", status.read_text())
            self.assertIn("reason=uboot_truncated", status.read_text())

    def test_stream_syncs_kernel_events_and_reports_status_without_waiting_for_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, cache, kernel, status = (root / name for name in ("pstore", "cache", "kmsg", "status"))
            source.mkdir()
            cache.mkdir()
            os.mkfifo(kernel)
            writer = os.open(kernel, os.O_RDWR | os.O_NONBLOCK)
            process = subprocess.Popen([str(self.binary), "--stream", str(source), str(cache),
                                        "boot-001", str(kernel), str(status)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while (not status.exists() or "state=ok" not in status.read_text()) and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIsNone(process.poll())
                self.assertIn("state=ok", status.read_text())
                os.write(writer, b"6,1,100,-;streamed kernel event\n")
                output = cache / "rmx1901-diagnostics/current/kernel.current"
                deadline = time.monotonic() + 5
                while (not output.exists() or b"streamed kernel event" not in output.read_bytes()) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIn(b"streamed kernel event", output.read_bytes())
            finally:
                process.terminate()
                process.wait(timeout=5)
                os.close(writer)


if __name__ == "__main__":
    unittest.main()
