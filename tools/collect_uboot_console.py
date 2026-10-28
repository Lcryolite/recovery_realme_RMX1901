#!/usr/bin/env python3
"""Collect one identified U-Boot USB console without rebooting or flashing."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

GADGET_SERIAL = "RMX1901-CONSOLE"
ENV = {"PATH": os.defpath, "LC_ALL": "C"}


class CollectionError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_candidate(path: Path) -> tuple[str, str, str]:
    manifest_bytes = path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("target"), dict):
        raise CollectionError("candidate manifest has an invalid target")
    nonce = manifest.get("attempt_nonce")
    image = manifest.get("image")
    if (
        manifest.get("target", {}).get("device") != "RMX1901"
        or manifest.get("read_only_usb_console") is not True
        or not isinstance(nonce, str)
        or not re.fullmatch(r"usb-[A-Za-z0-9_-]{1,64}", nonce)
        or not isinstance(image, dict)
        or not isinstance(image.get("path"), str)
        or not isinstance(image.get("bytes"), int)
        or not isinstance(image.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", image["sha256"])
    ):
        raise CollectionError("candidate manifest is not a read-only RMX1901 USB console")
    image_path = Path(image["path"])
    if not image_path.is_absolute():
        image_path = path.parent / image_path
    if image_path.stat().st_size != image["bytes"] or sha256_file(image_path) != image["sha256"]:
        raise CollectionError("candidate boot image does not match its manifest")
    return nonce, image["sha256"], hashlib.sha256(manifest_bytes).hexdigest()


def run_fastboot(binary: str, args: list[str], deadline: float) -> subprocess.CompletedProcess:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CollectionError("USB console collection timed out")
    try:
        return subprocess.run(
            [binary, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=min(remaining, 5), env=ENV, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CollectionError("fastboot command timed out") from error


def collect(manifest: Path, output: Path, timeout: float) -> dict:
    nonce, image_sha, manifest_sha = load_candidate(manifest)
    binary = shutil.which("fastboot")
    if not binary:
        raise CollectionError("fastboot is not available")
    if output.exists() or output.is_symlink():
        raise CollectionError("capture directory already exists")
    deadline = time.monotonic() + timeout
    print("MONITOR_READY", flush=True)

    while True:
        devices = run_fastboot(binary, ["devices"], deadline)
        if devices.returncode:
            raise CollectionError("fastboot device enumeration failed")
        matches = [line.split() for line in devices.stdout.decode(errors="replace").splitlines()
                   if line.split() and line.split()[0] == GADGET_SERIAL]
        if len(matches) > 1:
            raise CollectionError("multiple U-Boot gadgets have the same identity")
        if matches and matches[0] in ([GADGET_SERIAL, "fastboot"],
                                      [GADGET_SERIAL, "Android", "Fastboot"]):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError("U-Boot USB gadget was not observed")
        time.sleep(min(0.25, remaining))

    identity = run_fastboot(binary, ["-s", GADGET_SERIAL, "getvar", "console-attempt"], deadline)
    lines = [line.strip() for line in identity.stdout.decode(errors="replace").splitlines() if line.strip()]
    matches = [line for line in lines if "console-attempt:" in line]
    completed = [line for line in lines if re.fullmatch(r"Finished\. Total time: [0-9]+(?:\.[0-9]+)?s", line)]
    if (identity.returncode or matches != [f"console-attempt: {nonce}"] or len(completed) != 1
            or lines[-1:] != completed or any(re.search(r"\b(?:failed|error)\b", line, re.I) for line in lines)):
        raise CollectionError("U-Boot attempt identity did not match the candidate")

    output.mkdir(mode=0o700)
    transcript = output / "console.log"
    fd = os.open(transcript, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    exit_code = None
    timed_out = False
    with os.fdopen(fd, "wb") as log:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("fastboot oem console", timeout)
            proc = subprocess.run(
                [binary, "-s", GADGET_SERIAL, "oem", "console"],
                stdout=log, stderr=subprocess.STDOUT, timeout=remaining, env=ENV, check=False,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
    data = transcript.read_bytes()
    marker = f"RMX1901_USB_CONSOLE attempt={nonce}".encode()
    marker_pattern = rb"(?m)^(?:\(bootloader\)\s*)?" + re.escape(marker) + rb"\r?$"
    captured = (not timed_out and exit_code == 0 and len(re.findall(marker_pattern, data)) == 1
                and re.search(rb"(?m)^OKAY\s*\[", data) is not None)
    metadata = {
        "schema": 1,
        "status": "captured" if captured else "unverified",
        "source": "uboot_usb_console",
        "candidate_nonce": nonce,
        "boot_instance_verified": False,
        "candidate_boot_sha256": image_sha,
        "candidate_manifest_sha256": manifest_sha,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "console": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
    }
    meta_fd = os.open(output / "collection.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(meta_fd, "w") as stream:
        json.dump(metadata, stream, indent=2)
        stream.write("\n")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--relay-serial", help="optionally relay to this Recovery after collection")
    parser.add_argument("--relay-timeout", type=float, default=120.0)
    args = parser.parse_args()
    if not 0 < args.timeout <= 300:
        parser.error("timeout must be greater than zero and at most 300 seconds")
    if not 0 < args.relay_timeout <= 300:
        parser.error("relay timeout must be greater than zero and at most 300 seconds")
    try:
        result = collect(args.manifest, args.output, args.timeout)
    except (CollectionError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"U-Boot console collection failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    if result["status"] != "captured":
        return 1
    if args.relay_serial:
        from relay_uboot_console import RelayError, relay
        print("RELAY_WAITING_FOR_RECOVERY", flush=True)
        try:
            relay(args.manifest, args.output, args.relay_serial, args.relay_timeout)
        except (RelayError, OSError, ValueError) as error:
            print(f"U-Boot relay failed: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
