"""Command-line entry point: parse a demo and write the HTML report."""

import sys
from pathlib import Path

from .parsing import collect, DemoParser, DEFAULT_HIGHLIGHT
from .render import render_html


def main() -> None:
    if len(sys.argv) not in (2, 3, 4):
        sys.exit(f"Usage: python {sys.argv[0]} <path-to-demo.dem> "
                 f"[HighlightPlayer] [RivalPlayer]")

    demo_path = Path(sys.argv[1])
    if not demo_path.is_file():
        sys.exit(f"File not found: {demo_path}")
    highlight = sys.argv[2] if len(sys.argv) >= 3 else DEFAULT_HIGHLIGHT
    # No rival given -> auto-pick the highlighted player's nemesis in collect().
    rival = sys.argv[3] if len(sys.argv) >= 4 else None

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
    out_path = output_dir / f"{demo_path.stem}.html"
    out_path.write_text(render_html(summary, rounds, demo_path.name, meta),
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
