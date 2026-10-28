import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


COLLECTOR = Path(__file__).parents[1] / "tools" / "collect_uboot_console.py"


def candidate_fixture(root, identity_nonce="usb-test123", marker_nonce="usb-test123"):
    binary = root / "bin"
    binary.mkdir()
    calls = root / "commands"
    fastboot = binary / "fastboot"
    fastboot.write_text(
        "#!/usr/bin/python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        f"with open({str(calls)!r}, 'a') as f:\n"
        "    f.write(' '.join(args) + '\\n')\n"
        "if args == ['devices']:\n"
        "    print('RMX1901-CONSOLE\\tfastboot')\n"
        "elif args == ['-s', 'RMX1901-CONSOLE', 'getvar', 'console-attempt']:\n"
        f"    print('console-attempt: {identity_nonce}', file=sys.stderr)\n"
        "    print('Finished. Total time: 0.000s', file=sys.stderr)\n"
        "elif args == ['-s', 'RMX1901-CONSOLE', 'oem', 'console']:\n"
        f"    print('(bootloader) RMX1901_USB_CONSOLE attempt={marker_nonce}', file=sys.stderr)\n"
        "    print('(bootloader) boot stage reached', file=sys.stderr)\n"
        "    print('OKAY [  0.001s]', file=sys.stderr)\n"
        "    print('Finished. Total time: 0.001s', file=sys.stderr)\n"
        "else:\n"
        "    sys.exit(9)\n"
    )
    fastboot.chmod(0o755)
    image = root / "boot.img"
    image.write_bytes(b"verified test image")
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "target": {"device": "RMX1901"},
        "attempt_nonce": "usb-test123",
        "read_only_usb_console": True,
        "image": {
            "path": str(image), "bytes": image.stat().st_size,
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        },
    }))
    return manifest, root / "capture", calls, dict(os.environ, PATH=f"{binary}:/usr/bin:/bin")


def run_collector(manifest, output, env):
    return subprocess.run(
        [sys.executable, "-B", str(COLLECTOR), "--manifest", str(manifest),
         "--output", str(output), "--timeout", "2"],
        capture_output=True, text=True, env=env,
    )


class UBootUsbConsoleTest(unittest.TestCase):
    def test_optionally_relays_a_capture_after_recovery_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, output, calls, env = candidate_fixture(root)
            adb_calls = root / "adb-commands"
            stored_hash = root / "adb-hash"
            adb = root / "bin/adb"
            adb.write_text(
                "#!/usr/bin/python3\nimport hashlib, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                f"with open({str(adb_calls)!r}, 'a') as f: f.write(' '.join(args) + '\\n')\n"
                "if args[2:] == ['get-state']: print('recovery')\n"
                "elif args[2:] == ['shell', 'getprop ro.bootmode']: print('recovery')\n"
                "elif args[2:] in (['shell', 'cat /proc/sys/kernel/random/boot_id'], "
                "['shell', 'cat /cache/recovery/rmx1901-diagnostics/current/boot-id']): print('ab-123')\n"
                f"elif args[2] == 'push': pathlib.Path({str(stored_hash)!r}).write_text(hashlib.sha256(pathlib.Path(args[3]).read_bytes()).hexdigest())\n"
                f"elif args[2] == 'shell' and args[3].startswith('sha256sum '): print(pathlib.Path({str(stored_hash)!r}).read_text() + '  /tmp/test')\n"
                "elif args[2] == 'shell': pass\n"
                "else: sys.exit(4)\n"
            )
            adb.chmod(0o755)
            result = subprocess.run(
                [sys.executable, "-B", str(COLLECTOR), "--manifest", str(manifest),
                 "--output", str(output), "--timeout", "2", "--relay-serial", "7b0c1c49",
                 "--relay-timeout", "5"], capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RELAY_WAITING_FOR_RECOVERY", result.stdout)
            self.assertIn("--import-uboot /cache/recovery", adb_calls.read_text())
            self.assertNotIn("reboot", adb_calls.read_text())
            self.assertEqual(len(calls.read_text().splitlines()), 3)

    def test_rejects_a_malformed_manifest_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"target": []}))
            result = subprocess.run(
                [sys.executable, "-B", str(COLLECTOR), "--manifest", str(manifest),
                 "--output", str(root / "capture"), "--timeout", "2"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("candidate manifest", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse((root / "capture").exists())

    def test_collects_an_identified_uboot_attempt_without_device_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, output, calls, env = candidate_fixture(Path(directory))
            result = run_collector(manifest, output, env)

            self.assertEqual(result.returncode, 0, result.stderr)
            data = (output / "console.log").read_bytes()
            meta = json.loads((output / "collection.json").read_text())
            self.assertIn(b"RMX1901_USB_CONSOLE attempt=usb-test123", data)
            self.assertEqual(meta["console"]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(meta["status"], "captured")
            self.assertFalse(meta["boot_instance_verified"])
            self.assertEqual((output / "console.log").stat().st_mode & 0o777, 0o600)
            self.assertEqual(calls.read_text().splitlines(), [
                "devices", "-s RMX1901-CONSOLE getvar console-attempt",
                "-s RMX1901-CONSOLE oem console",
            ])
            self.assertNotIn("boot stage reached", result.stdout)

    def test_does_not_attribute_a_console_marker_with_a_different_nonce(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, output, calls, env = candidate_fixture(
                Path(directory), marker_nonce="usb-test123-other"
            )
            result = run_collector(manifest, output, env)
            self.assertNotEqual(result.returncode, 0)
            meta = json.loads((output / "collection.json").read_text())
            self.assertEqual(meta["status"], "unverified")
            self.assertIn(b"usb-test123-other", (output / "console.log").read_bytes())

    def test_does_not_collect_from_a_different_candidate_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, output, calls, env = candidate_fixture(
                Path(directory), identity_nonce="usb-other"
            )
            result = run_collector(manifest, output, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertEqual(calls.read_text().splitlines(), [
                "devices", "-s RMX1901-CONSOLE getvar console-attempt",
            ])

    def test_rejects_a_changed_boot_image_before_accessing_fastboot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, output, calls, env = candidate_fixture(root)
            (root / "boot.img").write_bytes(b"changed")
            result = run_collector(manifest, output, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertFalse(calls.exists())


if __name__ == "__main__":
    unittest.main()
