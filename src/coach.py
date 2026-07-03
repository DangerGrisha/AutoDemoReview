#!/usr/bin/env python3
"""Layer 6 -- LLM coaching feedback, kept fully separate from the HTML report.

This does NOT touch the visualizer. It re-uses the existing parse pipeline
(`demoreview.parsing.collect`) to get the same enriched, per-player data the
page is built from -- Layer 4 breakdowns (buy-type win rates, head-to-head,
death weapons) and Layer 5 rule flags (risky duels, clutch attempts/wins) --
condenses it into a compact structured summary for the reference player (you),
and asks Claude for a single, honest, match-level coaching report.

    python src/coach.py demos/match.dem [HighlightPlayer] [RivalPlayer]

Requires an ANTHROPIC_API_KEY in a local .env file (see .env.example).
"""

import json
import os
import sys
from pathlib import Path

# The sibling `demoreview` package lives next to this script in src/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from demoreview import parsing
from demoreview.parsing import collect, DEFAULT_HIGHLIGHT

MODEL = "claude-sonnet-5"
BUY_TYPES = ("eco", "force", "full")


class CoachError(Exception):
    """A recoverable failure generating the report (missing key, no credits, ...).

    Raised instead of exiting so callers (e.g. the HTML pipeline) can catch it
    and carry on -- the standalone CLI turns it back into a clean sys.exit.
    """

SYSTEM_PROMPT = (
    "You are a direct, honest Counter-Strike 2 coach doing a one-on-one review "
    "of ONE player's own match -- the player whose stats you are given, not their "
    "team in general and not a generic audience. Speak to them in the second "
    "person ('you'). This is a personalised review grounded entirely in the data "
    "provided from a single match: do not invent numbers, do not pad with generic "
    "CS2 tips that aren't supported by the data, and don't hedge. Be blunt but "
    "constructive -- name the real weaknesses plainly and tie every point to a "
    "specific figure from the summary (a buy-type win rate, the head-to-head "
    "matchup, the risky-duel or clutch counts, the death weapons). If the data is "
    "thin on some dimension, say so rather than guessing."
)


def _aggregate(summary, meta):
    """Condense the enriched round data into a compact per-player summary dict.

    Reads the same Layer 4/5 blob the HTML embeds (`summary['analysis']`) and
    slices it down to just the reference player, aggregated across the match.
    """
    ref_sid = meta.get("ref_sid")
    analysis = summary.get("analysis", {})
    profile = analysis.get("profiles", {}).get(ref_sid)
    if profile is None:
        raise CoachError(
            f"No analysis profile for reference player '{meta.get('ref_name')}'."
        )

    stats = profile["stats"]

    # Buy-type win rates: collapse the per-side [won, lost] tallies to overall.
    win_by_buy = profile["winByBuy"]
    buy_win_rates = {}
    for b in BUY_TYPES:
        won = sum(win_by_buy[side][b][0] for side in ("CT", "T"))
        lost = sum(win_by_buy[side][b][1] for side in ("CT", "T"))
        total = won + lost
        buy_win_rates[b] = {
            "won": won, "lost": lost, "rounds": total,
            "win_pct": round(100.0 * won / total) if total else None,
        }

    # Weakest head-to-head: profile['h2h'] is pre-sorted worst-matchup-first.
    h2h = profile.get("h2h", [])
    weakest = h2h[0] if h2h else None
    worst_matchups = [
        {"opponent": m["name"], "you_killed_them": m["k"],
         "they_killed_you": m["d"], "net": m["diff"]}
        for m in h2h[:3]
    ]

    # Where you die: top weapons, and which of your team's buy types you die in.
    deaths_by_weapon = profile.get("deathsByWeapon", [])
    top_death_weapons = [
        {"weapon": w, "count": c} for w, c in deaths_by_weapon[:4]
    ]
    deaths_by_type = profile.get("deathsByType", {})
    worst_death_buytype = (
        max(deaths_by_type, key=deaths_by_type.get) if any(deaths_by_type.values())
        else None
    )

    # Layer 5 rule flags for the reference player, aggregated across the match.
    rules = analysis.get("rules", {})
    flag_rounds = {"risky-duel": [], "clutch-attempt": [], "clutch-won": []}
    for rnd in sorted(rules, key=int):
        for flag in rules[rnd].get(ref_sid, []):
            kind = flag.get("kind")
            if kind in flag_rounds:
                flag_rounds[kind].append(rnd)

    def _flag(kind):
        rounds_hit = flag_rounds[kind]
        return {"count": len(rounds_hit), "example_rounds": rounds_hit[:5]}

    return {
        "player": meta.get("ref_name"),
        "match_record": {
            "your_score": summary["my_final"],
            "opponent_score": summary["opp_final"],
            "rounds_played": summary["n_rounds"],
            "result": "win" if summary["won_match"] else "loss",
        },
        "your_stats": {
            "rating_hltv2": stats["rating"],
            "adr": stats["adr"],
            "kast_pct": stats["kast"],
            "kills": stats["kills"],
            "deaths": stats["deaths"],
            "assists": stats["assists"],
            "kd": stats["kd"],
            "headshot_pct": stats["hs_pct"],
            "opening_kills": stats["opening_k"],
            "opening_deaths": stats["opening_d"],
            "opening_duel_win_pct": stats["open_pct"],
            "multikill_rounds": stats["multi"],
            "trade_kills": stats["trade_kills"],
            "flash_assists": stats["flash_assists"],
        },
        "buy_type_win_rates": buy_win_rates,
        "weakest_matchup": (
            {"opponent": weakest["name"], "you_killed_them": weakest["k"],
             "they_killed_you": weakest["d"], "net": weakest["diff"]}
            if weakest else None
        ),
        "worst_matchups": worst_matchups,
        "death_patterns": {
            "top_death_weapons": top_death_weapons,
            "buy_type_you_die_in_most": worst_death_buytype,
            "deaths_by_your_buy_type": deaths_by_type,
        },
        "decision_flags": {
            "risky_duels": _flag("risky-duel"),
            "clutch_attempts": _flag("clutch-attempt"),
            "clutch_wins": _flag("clutch-won"),
        },
    }


def _build_report(agg):
    """Send the compact summary to Claude and return the coaching report text."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise CoachError(
            "python-dotenv is not installed. Run: pip install -r requirements.txt"
        )
    try:
        import anthropic
    except ImportError:
        raise CoachError(
            "anthropic is not installed. Run: pip install -r requirements.txt"
        )

    load_dotenv()  # pulls ANTHROPIC_API_KEY out of a local .env into the env
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise CoachError(
            "ANTHROPIC_API_KEY is not set. Create a .env file with:\n"
            "    ANTHROPIC_API_KEY=sk-ant-...\n"
            "(see .env.example) -- the key is never hardcoded."
        )

    user_prompt = (
        f"Here is the condensed data from one competitive match for {agg['player']}. "
        "It is already aggregated across the whole match -- treat every number as "
        "ground truth.\n\n"
        f"```json\n{json.dumps(agg, indent=2)}\n```\n\n"
        "Write me ONE cohesive, match-level coaching report, in markdown, with "
        "three short sections:\n"
        "1. **What you're doing well** -- the genuine strengths this data shows.\n"
        "2. **Where you're losing value** -- 2-3 concrete weaknesses, each grounded "
        "in a specific figure above (for example the weakest-matchup opponent, the "
        "risky-duel pattern from `decision_flags`, a weak buy-type win rate, or your "
        "most common death weapon).\n"
        "3. **What to work on next** -- 1-2 specific, actionable things to practise "
        "or change, following directly from the weaknesses.\n\n"
        "Keep it tight and readable -- lead with the point, then the evidence. No "
        "generic tips that the data doesn't support."
    )

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.AuthenticationError:
        raise CoachError(
            "Anthropic rejected the API key (401). Check ANTHROPIC_API_KEY in .env."
        )
    except anthropic.BadRequestError as exc:
        msg = str(exc)
        if "credit balance" in msg:
            raise CoachError(
                "Anthropic API call failed: your account has no credits.\n"
                "Add credits at https://console.anthropic.com -> Plans & Billing, "
                "then re-run. (Each report costs ~1-2 cents.)"
            )
        raise CoachError(f"Anthropic API call failed: {msg}")
    except anthropic.APIError as exc:
        raise CoachError(f"Anthropic API call failed: {exc}")
    # Sonnet 5 runs adaptive thinking by default; pull out only the text blocks.
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def generate_report_text(summary, meta):
    """Aggregate the enriched data and return the coaching report markdown.

    The single seam the HTML pipeline uses to embed the report at build time.
    Raises `CoachError` on any recoverable failure (missing key/deps, no
    credits, API error) so the caller can fall back gracefully.
    """
    return _build_report(_aggregate(summary, meta))


def main() -> None:
    if len(sys.argv) not in (2, 3, 4):
        sys.exit(f"Usage: python {sys.argv[0]} <path-to-demo.dem> "
                 f"[HighlightPlayer] [RivalPlayer]")

    demo_path = Path(sys.argv[1])
    if not demo_path.is_file():
        sys.exit(f"File not found: {demo_path}")
    highlight = sys.argv[2] if len(sys.argv) >= 3 else DEFAULT_HIGHLIGHT
    rival = sys.argv[3] if len(sys.argv) >= 4 else None

    print(f"Parsing {demo_path} ...")
    parser = parsing.DemoParser(str(demo_path))
    try:
        summary, rounds, meta = collect(parser, highlight, rival)
    except Exception as exc:
        sys.exit(f"Failed to parse demo: {exc}")
    if not summary:
        sys.exit("No scored rounds found in this demo.")

    note = "" if meta["found"] else f" (highlight '{highlight}' not found; used {meta['ref_name']})"
    print(f"Coaching review for: {meta['ref_name']}{note}")
    print(f"Asking {MODEL} for a coaching report ...")

    try:
        report = generate_report_text(summary, meta)
    except CoachError as exc:
        sys.exit(str(exc))

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    out_path = output_dir / "coaching_report.md"
    header = (
        f"# Coaching report -- {meta['ref_name']}\n\n"
        f"_Match: your team {summary['my_final']} - {summary['opp_final']} opponent "
        f"over {summary['n_rounds']} rounds._\n\n"
    )
    out_path.write_text(header + report + "\n", encoding="utf-8")

    print("\n" + "=" * 70)
    print(report)
    print("=" * 70)
    print(f"\nReport written to: {out_path.resolve()}")


if __name__ == "__main__":
    main()
