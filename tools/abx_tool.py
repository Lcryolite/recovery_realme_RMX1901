#!/usr/bin/env python3
"""Host runner and module bridge for recovery/root/system/bin/abx-tool."""

import importlib.util
from importlib.machinery import SourceFileLoader
import sys
from pathlib import Path

_ABX_TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "recovery"
    / "root"
    / "system"
    / "bin"
    / "abx-tool"
)

_loader = SourceFileLoader("abx_tool_core", str(_ABX_TOOL_PATH))
_spec = importlib.util.spec_from_loader("abx_tool_core", _loader)
if _spec is None:
    raise ImportError(f"Failed to create spec for abx-tool from {_ABX_TOOL_PATH}")

_mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(_mod)

# Re-export public symbols from abx-tool core
globals().update({k: v for k, v in _mod.__dict__.items() if not k.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(_mod.main(sys.argv[1:]))
