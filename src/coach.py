#!/usr/bin/env python3
"""Layer 6 -- LLM coaching feedback, kept fully separate from the HTML report.

This does NOT touch the visualizer. It re-uses the existing parse pipeline
(`demoreview.parsing.collect`) for the Layer 4/5 view of the match -- buy-type
win rates, head-to-head, death weapons, risky-duel and clutch flags -- and,
when the match has 3D replay artifacts, the Layer 7 tactical fact sheet
(`demoreview.tactics`): trading, spacing at death, flash outcomes, shots per
kill, all ranked against the other nine players in the same lobby.

    python src/coach.py demos/match.dem [HighlightPlayer] [RivalPlayer]

The model's job here is judgement and phrasing, not measurement. Everything it
is allowed to assert is computed beforehand and handed to it as ground truth;
the schema forces every criticism to name the rounds it came from, so the page
can link each point straight into the 3D replay at that round.

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
# The renderer lives in the package so the free coach never imports this module.
from demoreview.coachreport import LANGUAGE, render_markdown  # noqa: F401

# Opus is the default because the fact sheet rewards careful reading -- picking
# the two problems that actually cost rounds out of seventeen ranked metrics is
# the hard part. Sonnet is roughly a third of the price and still writes a good
# report; set COACH_MODEL in .env to trade quality for cost.
MODEL = os.environ.get("COACH_MODEL", "").strip() or "claude-opus-5"
BUY_TYPES = ("eco", "force", "full")

# Published list prices, USD per million tokens, for the per-report cost line.
# Unknown models simply report no cost rather than a wrong one.
PRICING = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# LANGUAGE comes from coachreport (one source of truth for both coaches); this
# maps it to the name the prompt asks the model to write in.
_LANG_NAMES = {"ru": "Russian", "en": "English"}


class CoachError(Exception):
    """A recoverable failure generating the report (missing key, no credits, ...).

    Raised instead of exiting so callers (e.g. the HTML pipeline) can catch it
    and carry on -- the standalone CLI turns it back into a clean sys.exit.
    """


SYSTEM_PROMPT = """\
You are a professional Counter-Strike 2 coach reviewing one player's match, \
one-on-one, the way a team coach reviews a demo with a player. You are given a \
measured fact sheet from that single match. Speak to the player directly ("you").

How to think about the data:

* Every number is measured from the demo. Treat it as ground truth. Never \
invent a number, a round, a position or an opponent that is not in the data.
* `vs_lobby` ranks the player against the other nine players in THAT match, \
where rank 1 is best and 10 is worst on that metric, with the direction already \
applied. Use it as your calibration: it tells you what is actually a weakness \
at this player's level instead of measuring them against professionals. A bad \
absolute number that ranks 2nd in the server is not this player's problem.
* Volume statistics (utility thrown, opening duels taken, trade chances) are \
deliberately left unranked. High volume is not automatically good or bad -- \
judge it by what it converted into.
* Separate symptom from cause. "You die early" is a symptom; "you take the \
first duel of the round before your utility is out" is a cause. Coach the cause.
* Distances are in Hammer units: a player is 72 units tall, 500 units is close \
support, 1500+ units is a different part of the map.
* The `rounds` array is the per-round timeline. Use it to find the concrete \
rounds behind a pattern, and cite those round numbers.
* If the data is thin or ambiguous on some dimension, say so plainly instead of \
guessing. A short honest report beats a padded one.

How to write it:

* Prioritise ruthlessly. Two or three problems that actually cost rounds, in \
the order they cost the most. Not a checklist.
* Every criticism names its evidence: the metric with its value and the lobby \
comparison, or the specific rounds. Every criticism ends in something the \
player can do differently, concrete enough to act on in the next match.
* Be direct and unsentimental, the way a good coach is. No hype, no hedging, \
no motivational filler. Do not pad the strengths section to be nice -- if the \
player was genuinely good at something, say it once and move on.
* Use CS2 vocabulary naturally (entry, trade, crossfire, off-angle, retake, \
default, pop-flash, spacing, timing).
"""


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


# The schema is what makes the report renderable rather than just readable:
# `rounds` on every problem is how the page links a criticism to the exact
# round in the 3D replay. Note the structured-output subset -- no minItems /
# maxItems -- so counts are asked for in the prompt instead.
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "One sentence: the honest read on this player's match.",
        },
        "strengths": {
            "type": "array",
            "description": "1-2 genuine strengths. Omit rather than pad.",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["point", "evidence"],
                "additionalProperties": False,
            },
        },
        "problems": {
            "type": "array",
            "description": "2-3 problems, most costly first.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "cause": {
                        "type": "string",
                        "description": "The habit behind it, not the symptom.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Metrics with values and the lobby comparison.",
                    },
                    "rounds": {
                        "type": "array",
                        "description": "Round numbers from the data showing this.",
                        "items": {"type": "integer"},
                    },
                    "fix": {"type": "string"},
                },
                "required": ["title", "severity", "cause", "evidence", "rounds", "fix"],
                "additionalProperties": False,
            },
        },
        "drill": {
            "type": "object",
            "description": "One practice task aimed at the top problem.",
            "properties": {
                "name": {"type": "string"},
                "how": {"type": "string"},
                "target": {
                    "type": "string",
                    "description": "A measurable goal for the next match.",
                },
            },
            "required": ["name", "how", "target"],
            "additionalProperties": False,
        },
    },
    "required": ["headline", "strengths", "problems", "drill"],
    "additionalProperties": False,
}

def _client():
    """Construct the Anthropic client, mapping setup problems to CoachError."""
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
    return anthropic, anthropic.Anthropic()


def _build_report(agg, facts):
    """Send the fact sheet to Claude and return the parsed report dict."""
    anthropic, client = _client()

    language = _LANG_NAMES.get(LANGUAGE, LANGUAGE)
    blocks = [
        "Match summary and per-player breakdown (Layers 4-5):",
        f"```json\n{json.dumps(agg, indent=2, ensure_ascii=False)}\n```",
    ]
    if facts:
        blocks += [
            "Tactical fact sheet measured from the 3D replay (Layer 7). "
            "`vs_lobby` is this player against the other nine in this match, "
            "rank 1 = best, direction already applied:",
            f"```json\n{json.dumps(facts, indent=2, ensure_ascii=False)}\n```",
        ]
    else:
        blocks.append(
            "No 3D replay artifacts for this match, so there is no positional, "
            "utility or trading data. Coach only what the summary above supports "
            "and say which dimensions you could not assess."
        )
    blocks.append(
        f"Write the report in {language}. Give 1-2 strengths and 2-3 problems, "
        "ordered by how much they cost. Cite round numbers from the data for "
        "each problem."
    )
    user_prompt = "\n\n".join(blocks)

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            # The system prompt is identical for every report, so caching it
            # makes each additional report ~90% cheaper on that prefix.
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            output_config={
                "effort": "high",
                "format": {"type": "json_schema", "schema": REPORT_SCHEMA},
            },
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
                "then re-run. (Each report costs a few cents.)"
            )
        raise CoachError(f"Anthropic API call failed: {msg}")
    except anthropic.APIError as exc:
        raise CoachError(f"Anthropic API call failed: {exc}")

    # A refusal returns 200 with empty/partial content -- guard before reading it.
    if response.stop_reason == "refusal":
        raise CoachError("The model declined to answer for this match.")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise CoachError(f"Empty response from {MODEL} (stop_reason="
                         f"{response.stop_reason}).")
    try:
        report = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CoachError(f"Could not parse the model's JSON report: {exc}")

    return report, _usage(response)


def _usage(response):
    """Token counts and the list-price cost of one report, for the UI.

    Cached input is billed at ~10% and cache writes at ~125%, which is why the
    system prompt carries a cache breakpoint: the second report onwards pays
    almost nothing for it.
    """
    u = response.usage
    fresh = getattr(u, "input_tokens", 0) or 0
    cached = getattr(u, "cache_read_input_tokens", 0) or 0
    written = getattr(u, "cache_creation_input_tokens", 0) or 0
    out = getattr(u, "output_tokens", 0) or 0
    record = {
        "input_tokens": fresh,
        "cache_read_tokens": cached,
        "cache_write_tokens": written,
        "output_tokens": out,
        "model": MODEL,
    }
    price = PRICING.get(MODEL)
    if price:
        in_rate, out_rate = price
        record["cost_usd"] = round(
            (fresh + written * 1.25 + cached * 0.1) / 1e6 * in_rate
            + out / 1e6 * out_rate, 4)
    return record


def generate_report(summary, meta, facts=None, with_usage=False):
    """Aggregate the enriched data and return the STRUCTURED coaching report.

    `facts` is the optional `demoreview.tactics.analyse` fact sheet; without it
    the report falls back to the Layer 4/5 aggregates alone. With `with_usage`
    the return becomes `(report, usage)` so callers can show what it cost.
    """
    report, usage = _build_report(_aggregate(summary, meta), facts)
    return (report, usage) if with_usage else report


def generate_report_text(summary, meta, facts=None, match_id=None):
    """Aggregate the enriched data and return the coaching report markdown.

    The single seam the HTML pipeline uses to embed the report at build time.
    Raises `CoachError` on any recoverable failure (missing key/deps, no
    credits, API error) so the caller can fall back gracefully.
    """
    return render_markdown(generate_report(summary, meta, facts), match_id)


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
