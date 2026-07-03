#!/usr/bin/env python3
"""Backwards-compatible entry point for demoReview.

The implementation now lives in the `demoreview` package (in this same `src/`
directory). This shim keeps the original invocation working:

    python src/parse_demo.py demos/match.dem [HighlightPlayer] [RivalPlayer]

Equivalently:

    python -m demoreview demos/match.dem [HighlightPlayer] [RivalPlayer]
"""

import sys
from pathlib import Path

# Make the sibling `demoreview` package importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from demoreview.cli import main

if __name__ == "__main__":
    main()
