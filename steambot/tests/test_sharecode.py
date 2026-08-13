"""Validate the sharecode codec against a known Valve test vector.

The round-trip test alone is insufficient: encode/decode are inverses of each
other for ANY dictionary, so a wrong dictionary still round-trips cleanly. Only
an external, known-good vector proves the decode matches Valve's real encoding.

Vector source: akiver/csgo-sharecode README (widely reproduced).
"""

import random

from steambot import sharecode

# (sharecode, matchid, outcomeid/reservationId, token/tvPort)
KNOWN_VECTOR = (
    "CSGO-GADqf-jjyJ8-cSP2r-smZRo-TO2xK",
    3230642215713767580,
    3230647599455273103,
    55788,
)


def test_dictionary_is_base_57():
    assert len(sharecode.DICTIONARY) == 57
    # The five ambiguous glyphs must be absent.
    for bad in ("I", "g", "l", "0", "1"):
        assert bad not in sharecode.DICTIONARY, f"{bad!r} must not be in dictionary"
    # O/o are kept.
    assert "O" in sharecode.DICTIONARY and "o" in sharecode.DICTIONARY


def test_decode_known_vector():
    code, mid, oid, tok = KNOWN_VECTOR
    got = sharecode.decode(code)
    assert got.matchid == mid, (got.matchid, mid)
    assert got.outcomeid == oid, (got.outcomeid, oid)
    assert got.token == tok, (got.token, tok)


def test_encode_known_vector():
    code, mid, oid, tok = KNOWN_VECTOR
    assert sharecode.encode(mid, oid, tok) == code


def test_round_trip_random():
    rng = random.Random(20260708)
    for _ in range(5000):
        m = rng.getrandbits(64)
        o = rng.getrandbits(64)
        t = rng.getrandbits(16)
        code = sharecode.encode(m, o, t)
        back = sharecode.decode(code)
        assert (back.matchid, back.outcomeid, back.token) == (m, o, t)


def test_is_valid_rejects_bad_chars():
    # 'l' and '0' are not in the dictionary -> invalid.
    assert not sharecode.is_valid("CSGO-lllll-00000-11111-IIIII-ggggg")
    assert sharecode.is_valid(KNOWN_VECTOR[0])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
    print("all sharecode tests passed")
