"""On-demand AI coaching reports, cached per (match, player).

The report is personalised: it is written about ONE player, so two participants
opening the same match each get their own. That is why the cache key includes
the viewer's SteamID and not just the match.

Storage mirrors the 3D replay convention -- a file on disk, existence is the
single source of truth, no DB column and therefore no migration. Reports are
small (a few kB of JSON) and regenerating one is a paid API call, so they are
written once and reused until the player explicitly asks for a fresh one.

Nothing here runs at import time and every failure is contained: the match page
renders exactly as before when the API key is missing or the call fails.
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from . import config, pipeline, replay3d

# src/ is already on sys.path via pipeline, but coach.py sits directly in src/
# (next to the demoreview package) so it is imported by bare name.
sys.path.insert(0, str(config.REPO_ROOT / "src"))

from demoreview import coachreport, coachrules, tactics  # noqa: E402

log = logging.getLogger("web.coaching")

# "local" is the rule engine: free, offline, no account, no per-report cost.
# "api" is the LLM coach in src/coach.py -- better at fusing metrics into a
# single root cause, but every report is a paid Anthropic call, so it stays
# opt-in via .env and is never the default.
BACKEND = (os.environ.get("COACH_BACKEND", "").strip() or "local").lower()

REPORTS_DIR = config.DATA_DIR / "coach"

# SteamID64s are digits, but the cache key is built from request-derived data,
# so it is validated rather than trusted.
_SID_RE = re.compile(r"^\d{5,20}$")


class CoachUnavailable(Exception):
    """Report could not be generated; the message is safe to show the user."""


def _key(match_id, sid):
    sid = str(sid)
    if not _SID_RE.match(sid):
        raise CoachUnavailable("Invalid player id.")
    return f"{int(match_id)}_{sid}.json"


def report_path(match_id, sid):
    return REPORTS_DIR / _key(match_id, sid)


def load(match_id, sid):
    """The cached report record, or None."""
    try:
        path = report_path(match_id, sid)
    except CoachUnavailable:
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("unreadable coaching report at %s", path)
        return None


def markdown(match_id, sid):
    """Cached report rendered to markdown, or None when there isn't one."""
    record = load(match_id, sid)
    if not record:
        return None
    try:
        md = coachreport.render_markdown(record["report"], match_id,
                                         record.get("seek"))
    except Exception:
        log.exception("failed to render cached report for match %s", match_id)
        return None
    # Provenance line. When a paid backend produced it, show what it cost --
    # hiding API spend from the person paying for it is how a per-match cost
    # quietly becomes a per-month surprise.
    usage = record.get("usage") or {}
    bits = [record.get("model", "?"), record.get("generated_at", "")]
    if usage.get("cost_usd") is not None:
        bits.append(f"${usage['cost_usd']:.3f}")
    return f"{md}\n\n`{' · '.join(b for b in bits if b)}`\n"


def _perspective(meta, sid, summary):
    """`meta` re-pointed at `sid` so the report is about the viewer.

    The stored blob is baked from the uploader's perspective, but Layer 4/5
    computes a profile for every player -- so any participant can be made the
    reference player without re-parsing anything.
    """
    profiles = (summary.get("analysis") or {}).get("profiles") or {}
    profile = profiles.get(str(sid))
    if profile is None:
        raise CoachUnavailable("You did not play in this match.")
    out = dict(meta)
    out["ref_sid"] = str(sid)
    out["ref_name"] = profile.get("name") or meta.get("ref_name")
    return out


def generate(match_id, sid, data_json):
    """Build and cache a coaching report for `sid` in this match.

    `data_json` is the match row's stored enriched blob -- no demo re-parse and
    no second DB read. Raises CoachUnavailable with a user-safe message.
    """
    summary, _rounds, meta = pipeline.loads_enriched(data_json)
    meta = _perspective(meta, sid, summary)

    facts = None
    if replay3d.has_replay(match_id):
        try:
            facts = tactics.analyse(replay3d.artifacts_dir(match_id), sid)
        except Exception:
            # Positional analysis is an enhancement for the LLM coach, but the
            # whole substance of the free one -- handled per backend below.
            log.exception("tactics analysis failed for match %s", match_id)

    started = time.time()
    usage = None
    if BACKEND == "local":
        if facts is None:
            raise CoachUnavailable(
                "Для этого матча нет 3D-данных, а бесплатный разбор строится "
                "именно на них. Матчи, импортированные ботом, их получают.")
        try:
            report = coachrules.build_report(facts)
        except Exception as exc:
            log.exception("rule-based report failed for match %s", match_id)
            raise CoachUnavailable(f"Не удалось собрать разбор: {exc}") from exc
        model = "local-rules"
    else:
        import coach
        try:
            report, usage = coach.generate_report(summary, meta, facts,
                                                  with_usage=True)
        except coach.CoachError as exc:
            raise CoachUnavailable(str(exc)) from exc
        except Exception as exc:
            log.exception("coaching report failed for match %s", match_id)
            raise CoachUnavailable(f"Coaching report failed: {exc}") from exc
        model = coach.MODEL

    # Round -> the second this player died, so a cited round links straight to
    # the moment rather than to the round start.
    seek = {str(r["n"]): r["death_s"]
            for r in (facts or {}).get("rounds", []) if r.get("death_s")}

    record = {
        "match_id": int(match_id),
        "steamid64": str(sid),
        "model": model,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "took_s": round(time.time() - started, 1),
        "used_3d": facts is not None,
        "usage": usage,
        "seek": seek,
        "report": report,
    }
    path = report_path(match_id, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a crash mid-write can't leave a half-parsed report.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    log.info("coaching report for match %s/%s in %.0fs (3d=%s)",
             match_id, sid, record["took_s"], record["used_3d"])
    return record
