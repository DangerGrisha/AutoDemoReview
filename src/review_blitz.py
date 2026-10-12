#!/usr/bin/env python3
"""Entry point for the World of Tanks Blitz analyzer, runnable from the repo root:

    python src/review_blitz.py replay.wotbreplay [--json out.json]

Equivalent to `python -m blitzreview ...` with src/ on the path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blitzreview.cli import main

if __name__ == "__main__":
    main()
