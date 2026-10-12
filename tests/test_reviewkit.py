"""Tests for reviewkit, the pieces both analyzers share (src/reviewkit/).

Everything here sits between an uploaded file and a page, so the first checks
are about escaping: a hostile nickname must come out as text, inside HTML and
inside an embedded JSON block alike. Then the lobby ranking both coaches
build on, the markdown of the CS2 coach (its one allowed link shape), and the
UI merge's promise that the old import paths are the shared objects -- plus
the CS2 report renderer importing without the demo parser installed, which it
could not do before (it pulled in parsing.py, which exits without
demoparser2).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

from reviewkit import html as kit_html
from reviewkit import markdown, ranking, theme

ROOT = Path(__file__).resolve().parent.parent
EVIL = '</script><script>alert(1)</script><!-- "quoted" & <img src=x>'


def test_esc_escapes_quotes_and_drops_none():
    assert kit_html.esc(EVIL).count("<") == 0 and "&quot;quoted&quot;" in kit_html.esc(EVIL)
    assert kit_html.esc(None) == "" and kit_html.esc(0) == "0"


def test_embedded_json_cannot_end_its_script_block_and_reads_back():
    blob = kit_html.embed_json({"n": EVIL, "ru": "Каньон"})
    assert "<" not in blob and ">" not in blob and "&" not in blob
    assert "Каньон" in blob  # not \u-escaped: the page is UTF-8
    assert json.loads(blob) == {"n": EVIL, "ru": "Каньон"}


def test_rank_against_points_the_right_way_and_needs_a_lobby():
    assert ranking.rank_against(50, [10, 20, 30, 40, 60, 70])["rank"] == 3
    assert ranking.rank_against(50, [10, 20, 30, 40, 60, 70], higher_better=False)["rank"] == 5
    assert ranking.rank_against(5, [5, 5, 1, 0])["rank"] == 1          # ties share the better rank
    assert ranking.rank_against(3, [1, 2, 4, 5])["median"] == 3          # median of the others
    assert ranking.rank_against(3, [1, 2, 4]) is None                    # too few to compare
    assert ranking.rank_against(None, [1, 2, 3, 4]) is None
    assert ranking.rank_against(3, [True, False, None, 1, 2, 4, 5])["of"] == 5  # not numbers: dropped


def test_markdown_escapes_first_and_allows_only_the_given_link():
    link = (re.compile(r"\[(\d+)\]\((/r/\d+)\)"), r'<a href="\2">\1</a>')
    out = markdown.markdown_to_html("# Head <b>\n- **bold** [4](/r/4) [x](javascript:alert(1))\n"
                                    "1. `code`\n\npara " + EVIL, link)
    assert "<h3>Head &lt;b&gt;</h3>" in out and "<strong>bold</strong>" in out
    assert '<a href="/r/4">4</a>' in out and "javascript:alert" in out and 'href="javascript' not in out
    assert "<ol><li><code>code</code></li></ol>" in out and "<script>" not in out
    assert "<a " not in markdown.markdown_to_html("[4](/r/4)")         # no link shape: plain text


def test_theme_tokens_can_be_renamed_for_a_page():
    css = theme.css_vars({"panel": "card"}, fonts=False)
    assert css.startswith("--bg:#0B0E11;--card:#12161B") and "--panel:" not in css
    assert "--display:" in theme.css_vars()


def test_the_old_import_paths_are_the_shared_objects():
    sys.path.insert(0, str(ROOT / "src"))
    from blitzreview import analysis as blitz_analysis
    from blitzreview import render as blitz_render
    from demoreview import render as cs2_render
    assert cs2_render.esc is kit_html.esc and cs2_render.embed_json is kit_html.embed_json
    assert blitz_render.esc is kit_html.esc and blitz_render.embed_json is kit_html.embed_json
    assert blitz_analysis.rank_against is ranking.rank_against


def test_the_cs2_renderer_imports_without_the_demo_parser():
    code = ("import sys; sys.modules['demoparser2'] = None; sys.modules['pandas'] = None; "
            "import demoreview.render as r; "
            "assert 'demoreview.parsing' not in sys.modules, 'render pulled in parsing'; "
            "print(r.buy_type(2000))")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT / "src"),
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr or done.stdout
    assert done.stdout.strip() == "force"
