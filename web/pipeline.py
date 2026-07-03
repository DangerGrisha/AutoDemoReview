"""Bridge to the existing demoReview pipeline (imported, not duplicated).

Puts src/ on sys.path exactly like src/parse_demo.py does, then re-exports the
three reuse seams: parse a .dem, (de)serialize the enriched blob, and render the
match HTML. A global semaphore bounds concurrent parses so several large demos
can't OOM the box.
"""

import json
import sys
import threading
from pathlib import Path

# web/ is a sibling of src/; make `demoreview` importable by bare name.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from demoreview.parsing import collect, DemoParser, DEFAULT_HIGHLIGHT  # noqa: E402
from demoreview.render import render_html  # noqa: E402

from . import config  # noqa: E402

# Bound concurrent parses (each 300-500 MB demo uses substantial RAM).
_parse_gate = threading.BoundedSemaphore(config.PARSE_CONCURRENCY)


def parse_demo_file(dem_path, highlight_sid):
    """Parse the .dem at `dem_path`, spotlighting the uploader by SteamID64.

    Returns (summary, rounds, meta). `summary` is None / falsy when the demo has
    no scored rounds (the caller rejects those). Raises on a corrupt/invalid demo.
    Serialized via the module semaphore.
    """
    with _parse_gate:
        parser = DemoParser(str(dem_path))
        return collect(parser, DEFAULT_HIGHLIGHT, None, highlight_sid=highlight_sid)


def _json_default(o):
    # numpy scalars expose .item(); everything else falls back to str.
    return o.item() if hasattr(o, "item") else str(o)


def dumps_enriched(summary, rounds, meta):
    """Serialize the full enriched tuple for storage (numpy-safe)."""
    return json.dumps({"summary": summary, "rounds": rounds, "meta": meta},
                      default=_json_default)


def loads_enriched(text):
    """Inverse of dumps_enriched -> (summary, rounds, meta)."""
    d = json.loads(text)
    return d["summary"], d["rounds"], d["meta"]


def render_match_html(summary, rounds, demo_name, meta, site_nav=None):
    """Regenerate the self-contained match HTML from stored data.

    `site_nav` is an optional HTML snippet the web app injects at the top of the
    report (back-to-profile nav); the CLI path never passes it.
    """
    return render_html(summary, rounds, demo_name, meta, coaching_md=None, site_nav=site_nav)
