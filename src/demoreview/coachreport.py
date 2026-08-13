"""Rendering for a coaching report, independent of who produced it.

Both coaches emit the same dict -- the free rule engine (coachrules.py) and the
optional LLM one (src/coach.py) -- so the markdown, the severity labels and the
round deep-links into the 3D replay live here, in one place, with no dependency
on the Anthropic SDK.
"""

import os

# Reports are written in the reader's language, not the codebase's. The free
# rule engine currently only speaks Russian; this switch covers the headings.
LANGUAGE = os.environ.get("COACH_LANGUAGE", "ru").strip() or "ru"

SEVERITY_LABEL = {
    "ru": {"high": "критично", "medium": "заметно", "low": "мелочь"},
    "en": {"high": "critical", "medium": "notable", "low": "minor"},
}

HEADINGS = {
    "ru": ("Что работает", "Что стоит раундов", "Что тренировать", "раунды",
           "Причина", "Данные", "Что делать", "Цель"),
    "en": ("What works", "What costs you rounds", "What to practise", "rounds",
           "Cause", "Evidence", "Do this", "Target"),
}


def render_markdown(report, match_id=None, seek=None):
    """Render a structured report to the markdown the HTML card expects.

    When `match_id` is given, each problem's round list becomes links into the
    3D replay at that round -- the whole point of making findings cite rounds.
    `seek` maps round number -> seconds (the player's death), so the link lands
    on the moment being criticised instead of the round start.
    """
    seek = seek or {}

    def _round_link(n):
        n = int(n)
        if match_id is None:
            return str(n)
        url = f"/matches/{int(match_id)}/3d?round={n}"
        at = seek.get(n, seek.get(str(n)))
        if at:
            url += f"&t={float(at):.1f}"
        return f"[{n}]({url})"

    sev = SEVERITY_LABEL.get(LANGUAGE, SEVERITY_LABEL["en"])
    h_ok, h_bad, h_drill, h_rounds, h_cause, h_evid, h_fix, h_target = \
        HEADINGS.get(LANGUAGE, HEADINGS["en"])

    out = [f"**{report.get('headline', '').strip()}**", ""]

    strengths = report.get("strengths") or []
    if strengths:
        out.append(f"### {h_ok}")
        for s in strengths:
            out.append(f"- **{s.get('point','')}** — {s.get('evidence','')}")
        out.append("")

    problems = report.get("problems") or []
    if problems:
        out.append(f"### {h_bad}")
        for p in problems:
            label = sev.get(p.get("severity", ""), p.get("severity", ""))
            out.append(f"#### {p.get('title','')} ({label})")
            out.append(f"- **{h_cause}:** {p.get('cause','')}")
            out.append(f"- **{h_evid}:** {p.get('evidence','')}")
            rounds = p.get("rounds") or []
            if rounds:
                links = ", ".join(_round_link(n) for n in rounds)
                out.append(f"- **{h_rounds.capitalize()}:** {links}")
            out.append(f"- **{h_fix}:** {p.get('fix','')}")
            out.append("")

    drill = report.get("drill") or {}
    if drill:
        out.append(f"### {h_drill}")
        out.append(f"**{drill.get('name','')}** — {drill.get('how','')}")
        if drill.get("target"):
            out.append("")
            out.append(f"_{h_target}: {drill['target']}_")

    return "\n".join(out).strip() + "\n"
