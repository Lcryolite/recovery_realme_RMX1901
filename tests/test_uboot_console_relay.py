import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
RELAY = ROOT / "tools/relay_uboot_console.py"


def fixture(root):
    image = root / "boot.img"
    image.write_bytes(b"candidate")
    manifest = root / "manifest.json"
    image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest.write_text(json.dumps({
        "target": {"device": "RMX1901"},
        "attempt_nonce": "usb-fixture",
        "read_only_usb_console": True,
        "image": {"path": str(image), "bytes": image.stat().st_size, "sha256": image_sha},
    }))
    capture = root / "capture"
    capture.mkdir()
    log = b"(bootloader) RMX1901_USB_CONSOLE attempt=usb-fixture\n(bootloader) diagnostic\nOKAY [  0.001s]\n"
    (capture / "console.log").write_bytes(log)
    (capture / "collection.json").write_text(json.dumps({
        "status": "captured", "source": "uboot_usb_console", "candidate_nonce": "usb-fixture",
        "boot_instance_verified": False, "candidate_boot_sha256": image_sha,
        "candidate_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "exit_code": 0, "timed_out": False,
        "console": {"bytes": len(log), "sha256": hashlib.sha256(log).hexdigest()},
    }))
    return manifest, capture


class UBootConsoleRelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "tools"))
        spec = importlib.util.spec_from_file_location("relay_uboot_console", RELAY)
        cls.relay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.relay)

    def test_accepts_only_the_matching_complete_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, capture = fixture(Path(directory))
            nonce, digest = self.relay.validate_capture(manifest, capture)
            self.assertEqual(nonce, "usb-fixture")
            self.assertEqual(digest, hashlib.sha256((capture / "console.log").read_bytes()).hexdigest())

    def test_rejects_a_changed_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, capture = fixture(Path(directory))
            (capture / "console.log").write_bytes(b"wrong")
            with self.assertRaises(self.relay.RelayError):
                self.relay.validate_capture(manifest, capture)

    def test_rejects_an_unverified_or_mismatched_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, capture = fixture(Path(directory))
            path = capture / "collection.json"
            metadata = json.loads(path.read_text())
            metadata["status"] = "unverified"
            path.write_text(json.dumps(metadata))
            with self.assertRaises(self.relay.RelayError):
                self.relay.validate_capture(manifest, capture)
            metadata["status"] = "captured"
            metadata["candidate_nonce"] = "usb-other"
            path.write_text(json.dumps(metadata))
            with self.assertRaises(self.relay.RelayError):
                self.relay.validate_capture(manifest, capture)

    def test_invalid_capture_never_contacts_adb(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, capture = fixture(root)
            (capture / "console.log").write_bytes(b"invalid")
            result = subprocess.run([sys.executable, "-B", str(RELAY), "--manifest", str(manifest),
                                     "--capture", str(capture), "--serial", "7b0c1c49", "--timeout", "1"],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("capture", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_relay_verifies_device_copy_then_imports_without_rebooting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, capture = fixture(root)
            digest = hashlib.sha256((capture / "console.log").read_bytes()).hexdigest()
            binary = root / "bin"
            binary.mkdir()
            calls = root / "calls.jsonl"
            adb = binary / "adb"
            adb.write_text(
                "#!/usr/bin/python3\nimport json, sys\n"
                f"with open({str(calls)!r}, 'a') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "args = sys.argv[1:]\n"
                "if args[2:] == ['get-state']: print('recovery')\n"
                "elif args[2:] == ['shell', 'getprop ro.bootmode']: print('recovery')\n"
                "elif args[2:] == ['shell', 'cat /proc/sys/kernel/random/boot_id']: print('ab-123')\n"
                "elif args[2:] == ['shell', 'cat /cache/recovery/rmx1901-diagnostics/current/boot-id']: print('ab-123')\n"
                f"elif len(args) == 4 and args[2] == 'shell' and args[3].startswith('sha256sum '): print({digest!r} + '  /tmp/console.log')\n"
                "elif args[2] in ('shell', 'push'): pass\n"
                "else: sys.exit(4)\n"
            )
            adb.chmod(0o755)
            env = dict(os.environ, PATH=f"{binary}:/usr/bin:/bin")
            result = subprocess.run([sys.executable, "-B", str(RELAY), "--manifest", str(manifest),
                                     "--capture", str(capture), "--serial", "7b0c1c49", "--timeout", "5"],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = [json.loads(line) for line in calls.read_text().splitlines()]
            commands = [" ".join(args) for args in arguments]
            self.assertTrue(any("--import-uboot /cache/recovery" in command for command in commands))
            self.assertTrue(any("cat /proc/sys/kernel/random/boot_id" in command for command in commands))
            self.assertTrue(any("ab-123 /tmp/rmx1901-diagnostics.status" in command for command in commands))
            self.assertTrue(any("sha256sum " in command for command in commands))
            self.assertTrue(any("rm -rf /tmp/rmx1901-uboot-relay-" in command for command in commands))
            self.assertFalse(any("reboot" in command or " flash " in command for command in commands))
            self.assertFalse(json.loads(result.stdout)["boot_instance_verified"])


if __name__ == "__main__":
    unittest.main()
