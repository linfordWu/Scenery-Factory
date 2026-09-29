# SPDX-License-Identifier: GPL-3.0-only
"""Startup entry: python apps/api/__main__.py  (or: python -m apps.api)

The repository root is not a valid Python package name, so we alias it as
the ``svf`` package before importing the app (relative imports in the
codebase assume the root is a package).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if "svf" not in sys.modules:
    pkg = types.ModuleType("svf")
    pkg.__path__ = [str(ROOT)]
    sys.modules["svf"] = pkg

import os  # noqa: E402

import uvicorn  # noqa: E402

from svf.apps.api.app import build_default  # noqa: E402


def main() -> None:
    uvicorn.run(build_default(), host="0.0.0.0",
                port=int(os.environ.get("SVF_PORT", "8600")))


if __name__ == "__main__":
    main()
