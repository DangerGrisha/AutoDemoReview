"""Command-line entry point: parse a demo and write the HTML report."""

import sys
from pathlib import Path

from .parsing import collect, DemoParser, DEFAULT_HIGHLIGHT
from .render import render_html


def _resolve_coaching(with_coaching, summary, meta, cache_path):
    """Return the coaching-report markdown to bake into the page, or None.

    Keeps the two pipelines loosely coupled: the coach lives in src/coach.py and
    is only imported when actually needed.

      * --with-coaching: (re)generate via the paid Anthropic API and cache it.
        Any failure is non-fatal -- warn and fall back to the last cached report
        (else None), so the visualiser always still builds.
      * default run: skip the API entirely, reuse whatever was last generated
        for this demo (else None -> the page shows a placeholder).
    """
    def cached():
        return cache_path.read_text(encoding="utf-8") if cache_path.is_file() else None

    if not with_coaching:
        return cached()

    # coach.py sits in src/, next to (not inside) the demoreview package.
    src_dir = str(Path(__file__).resolve().parent.parent)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    import coach

    try:
        print(f"Generating coaching report via {coach.MODEL} (Anthropic API) ...")
        report = coach.generate_report_text(summary, meta)
        cache_path.write_text(report, encoding="utf-8")
        print(f"Coaching report generated and cached at {cache_path}.")
        return report
    except coach.CoachError as exc:
        print(f"[coaching] not generated: {exc}")
        fallback = cached()
        if fallback:
            print("[coaching] using the previously cached report instead.")
        return fallback


def main() -> None:
    args = sys.argv[1:]
    with_coaching = "--with-coaching" in args
    positional = [a for a in args if a != "--with-coaching"]

    if len(positional) not in (1, 2, 3):
        sys.exit(f"Usage: python {sys.argv[0]} [--with-coaching] "
                 f"<path-to-demo.dem> [HighlightPlayer] [RivalPlayer]")

    demo_path = Path(positional[0])
    if not demo_path.is_file():
        sys.exit(f"File not found: {demo_path}")
    highlight = positional[1] if len(positional) >= 2 else DEFAULT_HIGHLIGHT
    # No rival given -> auto-pick the highlighted player's nemesis in collect().
    rival = positional[2] if len(positional) >= 3 else None

    print(f"Parsing {demo_path} ...")
    parser = DemoParser(str(demo_path))
    try:
        summary, rounds, meta = collect(parser, highlight, rival)
    except Exception as exc:  # surface parse errors plainly for now
        sys.exit(f"Failed to parse demo: {exc}")

    if not summary:
        print("No scored rounds found in this demo.")
        return

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)

    # Layer 6: pre-generate (or reuse a cached) coaching report to bake into the
    # page. Never fatal -- returns None on failure and the page shows a placeholder.
    coaching_md = _resolve_coaching(
        with_coaching, summary, meta,
        output_dir / f"{demo_path.stem}.coaching.md")

    out_path = output_dir / f"{demo_path.stem}.html"
    out_path.write_text(
        render_html(summary, rounds, demo_path.name, meta, coaching_md=coaching_md),
        encoding="utf-8")

    note = "" if meta["found"] else f" (highlight '{highlight}' not found; used {meta['ref_name']})"
    if meta["rival_found"] and meta["rival_auto"]:
        rival_note = f"; nemesis: {meta['rival_name']} (killed you {meta['rival_kills_on_me']}x)"
    elif meta["rival_found"]:
        rival_note = f"; rival: {meta['rival_name']}"
    else:
        rival_note = f"; rival '{rival}' not found (filter hidden)" if rival else "; no nemesis found"
    print(f"Final: Your team {summary['my_final']} - {summary['opp_final']} Opponent "
          f"[{meta['ref_name']}]{note}{rival_note}")
    print(f"Report written to: {out_path.resolve()}")
    print(f"Open it with:  open {out_path}")


if __name__ == "__main__":
    main()
