"""CS2 match sharing code codec.

A sharecode looks like ``CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK``. It decodes
deterministically (no network, no keys) into the three integers the Game
Coordinator needs to look a match up:

    matchid    (uint64)  -- the match reservation / match id
    outcomeid  (uint64)  -- the "outcome" / reservation id
    token      (uint16)  -- the TV port / confirmation token

The scheme is a big-integer over a 57-character dictionary; the 18 packed bytes
are little-endian per field. ``decode`` and ``encode`` are exact inverses --
verified both by a 10k-iteration round-trip and against an external Valve test
vector (see tests/test_sharecode.py).

Ported from the reference implementations (ValvePython/csgo, akiver/csgo-sharecode,
DoctorMcKay/node-globaloffensive), which agree byte-for-byte and are unchanged
between CS:GO and CS2 -- the sharecode format did not change in the CS2 move.

The dictionary is a base-57 alphabet: the full alphanumerics MINUS the five
ambiguous glyphs uppercase ``I``, lowercase ``g``, lowercase ``l``, and digits
``0`` and ``1`` (``O``/``o`` are kept). Getting this wrong -- e.g. leaving ``g``
in, which makes it base-58 -- silently yields wrong match ids, so it is pinned
and covered by a test against a known Valve sharecode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DICTIONARY = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhijkmnopqrstuvwxyz23456789"
_BASE = len(DICTIONARY)  # 57
assert _BASE == 57, f"sharecode dictionary must be 57 chars, got {_BASE}"

_SHARECODE_RE = re.compile(r"^CSGO(?:-[%s]{5}){5}$" % re.escape(DICTIONARY))


@dataclass(frozen=True)
class MatchId:
    """The decoded coordinates of a match, as the GC expects them."""

    matchid: int
    outcomeid: int
    token: int

    def __str__(self) -> str:
        return f"match={self.matchid} outcome={self.outcomeid} token={self.token}"


def is_valid(code: str) -> bool:
    """True if ``code`` is a syntactically valid sharecode (CSGO-xxxxx-...x5)."""
    return bool(_SHARECODE_RE.match(code.strip()))


def decode(code: str) -> MatchId:
    """Decode a ``CSGO-...`` sharecode into (matchid, outcomeid, token).

    Raises ValueError if the code is malformed.
    """
    code = code.strip()
    if not is_valid(code):
        raise ValueError(f"malformed sharecode: {code!r}")
    cleaned = code.replace("CSGO", "").replace("-", "")

    big = 0
    for ch in reversed(cleaned):
        big = big * _BASE + DICTIONARY.index(ch)

    buf = big.to_bytes(18, "big")
    return MatchId(
        matchid=int.from_bytes(buf[0:8], "little"),
        outcomeid=int.from_bytes(buf[8:16], "little"),
        token=int.from_bytes(buf[16:18], "little"),
    )


def encode(matchid: int, outcomeid: int, token: int) -> str:
    """Encode (matchid, outcomeid, token) back into a ``CSGO-...`` sharecode.

    Inverse of :func:`decode`. Handy for tests and for logging a canonical form.
    """
    buf = (
        int(matchid).to_bytes(8, "little")
        + int(outcomeid).to_bytes(8, "little")
        + int(token).to_bytes(2, "little")
    )
    big = int.from_bytes(buf, "big")

    chars = []
    for _ in range(25):
        big, rem = divmod(big, _BASE)
        chars.append(DICTIONARY[rem])
    s = "".join(chars)
    groups = [s[i : i + 5] for i in range(0, 25, 5)]
    return "CSGO-" + "-".join(groups)
