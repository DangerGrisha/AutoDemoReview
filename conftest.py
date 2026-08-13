"""Make the project importable from tests, whichever runner is used.

`demoreview` lives under src/ while `steambot` and `web` are top-level packages
at the repo root, so both directories have to be on sys.path before a test
imports anything. pytest picks this file up automatically; tests/run_tests.py
does the same by hand so the suite also runs with a bare `python` and no
third-party dependencies.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
