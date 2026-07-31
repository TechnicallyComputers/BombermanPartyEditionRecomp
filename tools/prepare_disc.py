#!/usr/bin/env python3
"""Thin wrapper — disc prepare lives in psxrecomp/tools/prepare_disc.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "psxrecomp" / "tools" / "prepare_disc.py"


def main() -> int:
    if not FW.is_file():
        print(f"framework prepare_disc missing: {FW}", file=sys.stderr)
        print("Init/update the psxrecomp submodule.", file=sys.stderr)
        return 1
    argv = sys.argv[1:]
    if "--config" not in argv:
        argv = [
            "--config",
            str(ROOT / "game.toml"),
            "--project-root",
            str(ROOT),
            *argv,
        ]
    return subprocess.call([sys.executable, str(FW), *argv])


if __name__ == "__main__":
    raise SystemExit(main())
