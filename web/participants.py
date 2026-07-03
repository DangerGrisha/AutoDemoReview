"""Extract per-player participant rows from a parsed match `summary`.

Pure transform (no DB), shared by the upload path and the migration backfill so
both produce identical rows. Output keys match the `match_participants` columns
in web/db.py. Works on both a fresh summary (int dict keys) and one round-tripped
through JSON (string keys), since data_json is what the backfill reads.
"""

import json
from collections import defaultdict


def is_real_steamid(sid):
    """True for a real SteamID64 (drops bots sid='0', spectators, phantom sids)."""
    return (isinstance(sid, str) and sid.isdigit()
            and len(sid) == 17 and sid.startswith("765611"))


def _weapons_for(summary, sid):
    """Kills-by-weapon for `sid` from the match kill log (excludes world/suicide)."""
    counts = defaultdict(int)
    for k in summary.get("map_kills", []):
        if k.get("killer_sid") == sid and k.get("victim_sid") != sid:
            counts[k.get("weapon") or "?"] += 1
    return dict(counts)


def extract_participants(summary):
    """Return a list of participant-row dicts, one per real player in the match.

    Each row is perspective-corrected for THAT player: my_score/opp_score/won are
    from their own side (inverted for the team opposite the uploader). Draws -> won=0.
    """
    my_final = int(summary.get("my_final", 0) or 0)
    opp_final = int(summary.get("opp_final", 0) or 0)
    n_rounds = int(summary.get("n_rounds", 0) or 0)
    profiles = (summary.get("analysis") or {}).get("profiles", {})

    rows = []
    for p in summary.get("players", []):
        sid = p.get("sid")
        if not is_real_steamid(sid):
            continue
        on_team = bool(p.get("on_your_team"))
        my_s, opp_s = (my_final, opp_final) if on_team else (opp_final, my_final)
        won = 1 if my_s > opp_s else 0                 # from the swapped score; draw -> 0
        multi = p.get("multi") or {}
        prof = profiles.get(sid) or {}
        rows.append({
            "steamid64": sid,
            "name": p.get("name"),
            "won": won, "my_score": my_s, "opp_score": opp_s,
            "rounds_played": int(p.get("rounds_in") or n_rounds or 1),
            "kills": int(p.get("kills", 0)), "deaths": int(p.get("deaths", 0)),
            "assists": int(p.get("assists", 0)), "hs": int(p.get("hs", 0)),
            "adr": float(p.get("adr", 0.0)), "kast": float(p.get("kast", 0.0)),
            "rating": float(p.get("rating", 0.0)), "hs_pct": float(p.get("hs_pct", 0.0)),
            "opening_k": int(p.get("opening_k", 0)), "opening_d": int(p.get("opening_d", 0)),
            "trade_kills": int(p.get("trade_kills", 0)),
            "clutch": int(p.get("clutch", 0)), "clutch_won": int(p.get("clutch_won", 0)),
            "flash_assists": int(p.get("flash_assists", 0)),
            "multi_json": json.dumps({str(k): int(v) for k, v in multi.items()}),
            "weapons_json": json.dumps(_weapons_for(summary, sid)),
            "h2h_json": json.dumps(prof.get("h2h") or []),
        })
    return rows
