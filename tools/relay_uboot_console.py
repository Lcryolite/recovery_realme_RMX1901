#!/usr/bin/env python3
"""Relay a verified host USB capture to an already-running RMX1901 Recovery."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from collect_uboot_console import CollectionError, load_candidate


class RelayError(Exception):
    pass


def validate_capture(manifest: Path, capture: Path) -> tuple[str, str]:
    try:
        nonce, image_sha, manifest_sha = load_candidate(manifest)
        metadata = json.loads((capture / "collection.json").read_bytes())
        transcript = capture / "console.log"
        if transcript.stat().st_size > 16 * 1024 * 1024:
            raise RelayError("capture exceeds the relay size limit")
        data = transcript.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        marker = rb"(?m)^(?:\(bootloader\)\s*)?" + re.escape(
            f"RMX1901_USB_CONSOLE attempt={nonce}".encode()
        ) + rb"\r?$"
        if (not isinstance(metadata, dict) or metadata.get("status") != "captured"
                or metadata.get("source") != "uboot_usb_console"
                or metadata.get("candidate_nonce") != nonce
                or metadata.get("candidate_boot_sha256") != image_sha
                or metadata.get("candidate_manifest_sha256") != manifest_sha
                or metadata.get("boot_instance_verified") is not False
                or metadata.get("exit_code") != 0 or metadata.get("timed_out") is not False
                or not isinstance(metadata.get("console"), dict)
                or metadata["console"].get("bytes") != len(data)
                or metadata["console"].get("sha256") != digest
                or len(re.findall(marker, data)) != 1
                or re.search(rb"(?m)^OKAY\s*\[", data) is None):
            raise RelayError("capture does not match its verified candidate and transcript")
        return nonce, digest
    except (CollectionError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RelayError(f"invalid capture: {error}") from error


def relay(manifest: Path, capture: Path, serial: str, timeout: float) -> None:
    nonce, digest = validate_capture(manifest, capture)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", serial):
        raise RelayError("invalid device serial")
    expected_serial = json.loads(manifest.read_text()).get("target", {}).get("serial")
    if expected_serial is not None and expected_serial != serial:
        raise RelayError("device serial does not match the candidate")
    adb = shutil.which("adb")
    if not adb:
        raise RelayError("adb is not available")
    environment = {"PATH": os.defpath, "LC_ALL": "C"}
    deadline = time.monotonic() + timeout

    def run(*args: str, check: bool = False) -> subprocess.CompletedProcess:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RelayError("Recovery relay timed out")
        try:
            result = subprocess.run([adb, "-s", serial, *args], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    timeout=min(remaining, 5), env=environment, check=False)
        except subprocess.TimeoutExpired as error:
            raise RelayError("adb command timed out") from error
        if check and result.returncode:
            raise RelayError("adb relay command failed")
        return result

    while True:
        state = run("get-state")
        if state.returncode == 0 and state.stdout.strip() == "recovery":
            mode = run("shell", "getprop ro.bootmode")
            available = run("shell", "test -x /system/bin/rmx1901-diagnostics")
            if mode.returncode == 0 and mode.stdout.strip() == "recovery" and available.returncode == 0:
                boot = run("shell", "cat /proc/sys/kernel/random/boot_id")
                current = run("shell", "cat /cache/recovery/rmx1901-diagnostics/current/boot-id")
                boot_id = boot.stdout.strip()
                if (boot.returncode == 0 and current.returncode == 0
                        and re.fullmatch(r"[a-f0-9-]{1,64}", boot_id)
                        and current.stdout.strip() == boot_id):
                    break
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))

    temporary = "/tmp/rmx1901-uboot-relay-" + uuid.uuid4().hex
    created = False
    try:
        run("shell", f"mkdir -m 700 {temporary}", check=True)
        created = True
        remote = temporary + "/console.log"
        run("push", str(capture / "console.log"), remote, check=True)
        run("shell", f"chmod 600 {remote}", check=True)
        observed = run("shell", f"sha256sum {remote}", check=True).stdout.split()
        if not observed or observed[0] != digest:
            raise RelayError("device-side temporary capture hash differs")
        run("shell", f"/system/bin/rmx1901-diagnostics --import-uboot /cache/recovery "
                     f"{remote} {nonce} {digest} {boot_id} /tmp/rmx1901-diagnostics.status", check=True)
    finally:
        if created:
            # This uniquely named directory was created by this process only.
            try:
                subprocess.run([adb, "-s", serial, "shell", f"rm -rf {temporary}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, env=environment, check=True)
            except (OSError, subprocess.SubprocessError):
                print(f"temporary relay cleanup failed: {temporary}", file=sys.stderr)
    print(json.dumps({"source": "uboot_usb_console", "candidate_nonce": nonce,
                      "reported_sha256": digest, "boot_instance_verified": False,
                      "destination": "/cache/recovery/rmx1901-diagnostics/current"}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--capture", required=True, type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    if not 0 < args.timeout <= 300:
        parser.error("timeout must be greater than zero and at most 300 seconds")
    try:
        relay(args.manifest, args.capture, args.serial, args.timeout)
    except (RelayError, OSError, ValueError) as error:
        print(f"U-Boot relay failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
