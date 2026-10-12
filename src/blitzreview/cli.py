"""Command line: python -m blitzreview <replay.wotbreplay> [--json out.json]

Prints a short Russian summary of the battle; --json also writes the full
battle dict (the same one the web app stores).
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import gamedata
from .battle import load
from .container import ReplayError

OUTCOME = {"win": "ПОБЕДА", "loss": "ПОРАЖЕНИЕ", "draw": "НИЧЬЯ", "unknown": "итог неизвестен"}


def _clock(seconds):
    if seconds is None:
        return "?"
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _tank(tank):
    tier = gamedata.tier_roman(tank["tier"])
    kind = gamedata.class_name(tank["type"]) or "?"
    return f"{tank['short']} ({tier} {kind})"


def summary(battle):
    lines = []
    started = (datetime.fromtimestamp(battle["started_at"]).strftime("%d.%m.%Y %H:%M")
               if battle["started_at"] else "?")
    mode = battle["mode"]["name"] if battle["mode"] else "режим неизвестен"
    lines.append(f"{battle['map']['name']} · {mode} · {battle['client_version']} · {started}")
    author = battle["author"]
    lines.append(f"{author['nickname']} — {_tank(author['tank'])} — "
                 f"{OUTCOME[battle['outcome']]}, бой {_clock(battle['duration'])}")
    for warning in battle["warnings"]:
        lines.append(f"  ! {warning}")

    names = {v["entity_id"]: v["nickname"] for v in battle["vehicles"]}
    for ally, title in ((True, "Союзники"), (False, "Противники")):
        lines.append("")
        lines.append(f"{title:<34}{'урон':>6}{'фраги':>7}{'выстр/попад/проб':>18}{'HP':>7}")
        for v in (v for v in battle["vehicles"] if v["ally"] == ally):
            s = v["stats"]
            mark = "*" if v["is_author"] else " "
            tag = f"[{v['clan_tag']}]" if v["clan_tag"] else ""
            name = f"{mark}{v['nickname']}{tag}"[:20]
            tank = v["tank"]["short"][:12]
            if s:
                shots = f"{s['shots']}/{s['hits']}/{s['penetrations']}"
                hp = "убит" if s["hp_left"] <= 0 else str(s["hp_left"])
                lines.append(f"{name:<21}{tank:<13}{s['damage']:>6}{s['kills']:>7}{shots:>18}{hp:>7}")
            else:
                state = "жив" if v["alive"] else ("убит" if v["alive"] is False else "?")
                lines.append(f"{name:<21}{tank:<13}{'—':>6}{'—':>7}{'—':>18}{state:>7}")
    if battle["kills"]:
        lines.append("")
        lines.append("Уничтожения:")
        for kill in battle["kills"]:
            killer = names.get(kill["killer"], "?") if kill["killer"] else "—"
            lines.append(f"  {_clock(kill['t'])}  {killer} → {names.get(kill['victim'], '?')}")
    return "\n".join(lines)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m blitzreview",
                                 description="World of Tanks Blitz replay analyzer")
    ap.add_argument("replay", type=Path)
    ap.add_argument("--json", type=Path, help="also write the full battle dict here")
    args = ap.parse_args(argv)

    if not args.replay.is_file():
        sys.exit(f"File not found: {args.replay}")
    try:
        battle = load(args.replay)
    except ReplayError as exc:
        sys.exit(f"Not a readable Blitz replay: {exc}")
    print(summary(battle))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(battle, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nJSON: {args.json}")
