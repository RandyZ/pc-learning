#!/usr/bin/env python3
"""One entrypoint: list / run <case numbers> / report <run directory>."""
from pathlib import Path
import sys

# Prefer this checkout, including when called outside the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from math_agent.evaltools.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
