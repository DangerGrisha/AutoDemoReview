"""Clear, step-numbered logging so a PoC run reads like a checklist.

The whole point of this proof-of-concept is to see *exactly* where the chain
works or breaks, so we favour loud, explicit, human-readable log lines over
structured logging. Every meaningful step gets a numbered ``step()`` line and an
``ok()`` / ``warn()`` / ``fail()`` outcome.
"""

from __future__ import annotations

import logging
import sys

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


class StepLogger:
    """A tiny wrapper that numbers steps and colourises outcomes."""

    def __init__(self, name: str = "steambot", verbose: bool = True) -> None:
        self._color = _supports_color()
        self._n = 0
        self._log = logging.getLogger(name)
        if not self._log.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
            self._log.addHandler(handler)
        self._log.setLevel(logging.DEBUG if verbose else logging.INFO)

    def _c(self, text: str, color: str) -> str:
        return f"{color}{text}{_RESET}" if self._color else text

    def step(self, msg: str) -> None:
        self._n += 1
        self._log.info(self._c(f"[step {self._n}] {msg}", _CYAN))

    def ok(self, msg: str) -> None:
        self._log.info("   " + self._c(f"OK    {msg}", _GREEN))

    def info(self, msg: str) -> None:
        self._log.info("   " + self._c(f"..    {msg}", _DIM))

    def warn(self, msg: str) -> None:
        self._log.warning("   " + self._c(f"WARN  {msg}", _YELLOW))

    def fail(self, msg: str) -> None:
        self._log.error("   " + self._c(f"FAIL  {msg}", _RED))

    def banner(self, msg: str) -> None:
        line = "=" * max(24, len(msg) + 4)
        self._log.info(self._c(line, _DIM))
        self._log.info(self._c(f"  {msg}", _CYAN))
        self._log.info(self._c(line, _DIM))
