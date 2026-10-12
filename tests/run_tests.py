"""Dependency-free test runner: `python tests/run_tests.py`.

pytest is not a project dependency (nothing else needs it, and the analysis code
these tests cover is pure stdlib), so the suite has to be runnable with the same
interpreter that runs the app. This walks the test directories, imports every
`test_*.py`, and calls every `test_*` function in it.

Every test file is also a plain pytest module -- same plain `assert`s, same
names -- so `python -m pytest` works unchanged wherever pytest IS installed.
A test may raise `unittest.SkipTest` to opt out (used by the checks that need
real 3D artifacts, which only exist on a machine that has parsed a demo).

Exit code is 1 if anything failed, so it can gate a commit or CI.
"""

import importlib.util
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Same job as the root conftest.py, done by hand: this runner is started as a
# script, so pytest's conftest loading never happens.
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

DEFAULT_DIRS = (ROOT / "tests", ROOT / "steambot" / "tests")


def _load(path):
    """Import a test file under a unique module name (paths may share basenames)."""
    name = f"_dr_tests_{path.parent.name}_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(dirs=DEFAULT_DIRS):
    passed = failed = skipped = 0
    failures = []

    for directory in dirs:
        directory = Path(directory).resolve()  # `tests` from the CLI is relative
        if not directory.is_dir():
            continue
        for path in sorted(Path(directory).glob("test_*.py")):
            try:
                module = _load(path)
            except Exception:
                failed += 1
                failures.append((f"{path.name} (import)", traceback.format_exc()))
                print(f"FAIL  {path.name} (import)")
                continue

            rel = path.relative_to(ROOT).as_posix()
            for fname in sorted(vars(module)):
                if not fname.startswith("test_"):
                    continue
                fn = getattr(module, fname)
                if not callable(fn):
                    continue
                try:
                    fn()
                except unittest.SkipTest as exc:
                    skipped += 1
                    print(f"SKIP  {rel}::{fname} -- {exc}")
                except Exception:
                    failed += 1
                    failures.append((f"{rel}::{fname}", traceback.format_exc()))
                    print(f"FAIL  {rel}::{fname}")
                else:
                    passed += 1
                    print(f"ok    {rel}::{fname}")

    for name, tb in failures:
        print(f"\n{'=' * 70}\nFAIL {name}\n{'-' * 70}\n{tb}")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    # Russian assertion messages must not die on a cp125x console.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = [Path(a) for a in sys.argv[1:]]
    sys.exit(run(args or DEFAULT_DIRS))
