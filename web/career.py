"""Career-stats aggregation across a user's linked matches.

Consumes the slim per-participant rows (db.career_totals + db.career_blobs) — it
never touches the multi-MB data_json.
"""

import json
from collections import defaultdict


def build_career(totals, blob_rows, name_lookup=None):
    """Assemble the career page dict. `totals` is db.career_totals(sid); `blob_rows`
    is db.career_blobs(sid); `name_lookup` optionally maps opponent sid -> display name."""
    matches = totals["matches"] if totals else 0
    if not matches:
        return {"matches": 0}

    kills, deaths = totals["kills"], totals["deaths"]
    rounds = totals["rounds"] or 1
    overall = {
        "matches": matches, "wins": totals["wins"], "losses": totals["losses"],
        "win_pct": round(100.0 * totals["wins"] / matches),
        "rounds": totals["rounds"],
        "kills": kills, "deaths": deaths, "assists": totals["assists"],
        "kd": round(kills / deaths, 2) if deaths else float(kills),
        "adr": round(totals["adr_w"] / rounds, 1),         # round-weighted
        "kast": round(totals["kast_w"] / rounds),
        "rating": round(totals["rating_w"] / rounds, 2),
        "hs_pct": round(100.0 * totals["hs"] / kills) if kills else 0,
    }

    # Weapon breakdown: merge kills-by-weapon across matches.
    weapons = defaultdict(int)
    for r in blob_rows:
        for w, c in json.loads(r["weapons_json"] or "{}").items():
            weapons[w] += int(c)
    top_weapons = sorted(weapons.items(), key=lambda kv: -kv[1])[:10]

    # Head-to-head: merge by opponent sid across matches.
    agg = {}
    for r in blob_rows:
        for e in json.loads(r["h2h_json"] or "[]"):
            osid = e.get("sid")
            if not osid:
                continue
            a = agg.setdefault(osid, {"name": e.get("name"), "k": 0, "d": 0})
            a["k"] += int(e.get("k", 0))
            a["d"] += int(e.get("d", 0))
            if e.get("name"):
                a["name"] = e["name"]
    h2h = [{
        "name": (name_lookup or {}).get(osid) or a["name"] or osid,
        "k": a["k"], "d": a["d"], "diff": a["k"] - a["d"],
    } for osid, a in agg.items()]
    h2h.sort(key=lambda x: x["diff"])                       # worst (most negative) first

    return {
        "matches": matches,
        "overall": overall,
        "top_weapons": top_weapons,
        "worst": h2h[:5],
        "best": list(reversed(h2h[-5:])) if h2h else [],
    }
