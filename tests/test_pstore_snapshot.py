import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).parents[1] / "recovery/diagnostics/pstore_snapshot.c"


class PstoreSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("cc"):
            raise unittest.SkipTest("C compiler unavailable")
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "pstore-snapshot"
        subprocess.run(["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                        str(SOURCE), "-o", str(cls.binary)], capture_output=True, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_preserves_distinct_raw_records_without_erasing_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "pstore", root / "archive"
            source.mkdir()
            destination.mkdir()
            samples = {
                "console-ramoops-0": ("console", b"console\x00tail\n"),
                "dmesg-ramoops-0": ("crash", b"panic\xffdata\n"),
                "pmsg-ramoops-0": ("pmsg", b"android\x00pmsg\n"),
            }
            for name, (_, data) in samples.items():
                (source / name).write_bytes(data)
            result = subprocess.run([str(self.binary), str(source), str(destination), "1024"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata = json.loads((destination / "records.json").read_text())
            self.assertEqual(metadata["status"], "captured")
            records = {record["source_name"]: record for record in metadata["records"]}
            self.assertEqual(set(records), set(samples))
            for name, (kind, data) in samples.items():
                self.assertEqual((source / name).read_bytes(), data)
                self.assertEqual((destination / records[name]["file"]).read_bytes(), data)
                self.assertEqual(records[name]["type"], kind)
                self.assertEqual(records[name]["origin"], "unknown")
                self.assertFalse(records[name]["truncated"])

    def test_refuses_an_existing_manifest_without_creating_new_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "pstore", root / "archive"
            source.mkdir()
            destination.mkdir()
            (source / "console-ramoops-0").write_bytes(b"new evidence")
            (destination / "records.json").write_bytes(b"existing evidence")
            result = subprocess.run([str(self.binary), str(source), str(destination), "1024"],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sorted(p.name for p in destination.iterdir()), ["records.json"])
            self.assertEqual((destination / "records.json").read_bytes(), b"existing evidence")

    def test_rejects_a_source_symlink_and_keeps_completed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "pstore", root / "archive"
            source.mkdir()
            destination.mkdir()
            (source / "dmesg-ramoops-0").write_bytes(b"crash")
            outside = root / "outside"
            outside.write_bytes(b"unrelated private data")
            (source / "console-ramoops-0").symlink_to(outside)
            (destination / "unrelated").write_bytes(b"keep me")
            result = subprocess.run([str(self.binary), str(source), str(destination), "1024"],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sorted(p.name for p in destination.iterdir()),
                             ["pstore-dmesg-ramoops-0.raw", "unrelated"])
            self.assertEqual((destination / "pstore-dmesg-ramoops-0.raw").read_bytes(), b"crash")
            self.assertEqual(outside.read_bytes(), b"unrelated private data")
            self.assertEqual((source / "dmesg-ramoops-0").read_bytes(), b"crash")

    def test_quota_keeps_crash_then_console_tail_and_marks_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "pstore", root / "archive"
            source.mkdir()
            destination.mkdir()
            (source / "dmesg-ramoops-0").write_bytes(b"0123456789")
            (source / "console-ramoops-0").write_bytes(b"abcdefghijkl")
            (source / "pmsg-ramoops-0").write_bytes(b"pmsg")
            result = subprocess.run([str(self.binary), str(source), str(destination), "15"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            metadata = json.loads((destination / "records.json").read_text())
            records = {record["type"]: record for record in metadata["records"]}
            self.assertEqual(metadata["status"], "partial")
            self.assertEqual((destination / records["crash"]["file"]).read_bytes(), b"0123456789")
            self.assertEqual((destination / records["console"]["file"]).read_bytes(), b"hijkl")
            self.assertTrue(records["console"]["truncated"])
            self.assertEqual(records["pmsg"]["file"], "")
            self.assertTrue(records["pmsg"]["truncated"])


if __name__ == "__main__":
    unittest.main()
