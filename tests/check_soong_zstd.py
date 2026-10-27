#!/usr/bin/env python3
"""Exercise Zstd through the synced Android build's actual PATH interposer."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("soong", type=Path)
    parser.add_argument("go", type=Path)
    args = parser.parse_args()
    soong = args.soong.resolve()
    go = args.go.resolve()
    zstd = shutil.which("zstd")
    if not zstd:
        raise SystemExit("zstd is not installed")
    zstd = str(Path(zstd).resolve())

    with tempfile.TemporaryDirectory(prefix="soong-zstd-check-") as directory:
        root = Path(directory)
        source = root / "source"
        for relative in (
            "cmd/path_interposer/main.go",
            "ui/build/paths/config.go",
            "ui/build/paths/logs.go",
        ):
            destination = source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(soong / relative, destination)
        (source / "go.mod").write_text("module android/soong\n\ngo 1.15\n")

        env = {
            "PATH": os.defpath,
            "LANG": "C",
            "GOCACHE": str(root / "gocache"),
            "GOPATH": str(root / "gopath"),
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "CGO_ENABLED": "0",
        }
        interposer = root / "path_interposer"
        subprocess.run(
            [str(go), "build", "-o", str(interposer), "./cmd/path_interposer"],
            cwd=source,
            env=env,
            check=True,
            timeout=60,
        )
        (root / "path_interposer_origpath").write_text(str(Path(zstd).parent))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "zstd").symlink_to(interposer)
        env["PATH"] = str(bin_dir)

        sample = b"Recovery ramdisk compression check\n" * 16
        result = subprocess.run(
            ["zstd", "-3", "-q", "-c"],
            input=sample,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=10,
        )
        if result.returncode:
            raise SystemExit(result.stderr.decode(errors="replace").strip())
        restored = subprocess.run(
            [zstd, "-d", "-q", "-c"],
            input=result.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
            timeout=10,
        ).stdout
        if restored != sample:
            raise SystemExit("Zstd round-trip did not preserve the ramdisk data")
    print("Soong PATH allows Zstd and the compression round-trip passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
