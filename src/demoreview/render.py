"""HTML/CSS/JS rendering of the report (single self-contained document)."""

import html
import json
import re

from . import maps
from .parsing import buy_type


def esc(text) -> str:
    return html.escape(str(text))


def _md_inline(text) -> str:
    """Escape text, then apply inline markdown (**bold**, `code`)."""
    out = esc(text)                                   # escape FIRST -> injection-safe
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    return out


def markdown_to_html(md: str) -> str:
    """Tiny, dependency-free markdown -> HTML for the coaching report.

    Handles the subset the model actually emits: ATX headers (#..######),
    unordered (-, *) and ordered (1.) lists, **bold**, `code`, and paragraphs.
    Every text run is escaped before any tag is inserted, so a player name or
    stray '<' in the report can't break out into the page.
    """
    parts, para, cur_list = [], [], None

    def flush_para():
        if para:
            parts.append("<p>" + _md_inline(" ".join(para)) + "</p>")
            para.clear()

    def close_list():
        nonlocal cur_list
        if cur_list:
            parts.append(f"</{cur_list}>")
            cur_list = None

    def open_list(kind):
        nonlocal cur_list
        if cur_list != kind:
            close_list()
            parts.append(f"<{kind}>")
            cur_list = kind

    for raw in (md or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush_para(); close_list(); continue

        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            flush_para(); close_list()
            lvl = len(h.group(1))
            tag = "h3" if lvl <= 1 else "h4" if lvl == 2 else "h5"
            parts.append(f"<{tag}>{_md_inline(h.group(2))}</{tag}>")
            continue

        ul = re.match(r"^[-*]\s+(.*)$", line)
        if ul:
            flush_para(); open_list("ul")
            parts.append(f"<li>{_md_inline(ul.group(1))}</li>")
            continue

        ol = re.match(r"^\d+\.\s+(.*)$", line)
        if ol:
            flush_para(); open_list("ol")
            parts.append(f"<li>{_md_inline(ol.group(1))}</li>")
            continue

        close_list()
        para.append(line)

    flush_para(); close_list()
    return "".join(parts)


def render_coaching(coaching_md) -> str:
    """The AI-coaching card: a toggle button + a hidden div with the baked report.

    `coaching_md` is the pre-generated markdown (from coach.generate_report_text),
    or None/empty when it wasn't generated -- in which case we show a static
    placeholder so the visualiser still renders fine.
    """
    if coaching_md and coaching_md.strip():
        body = markdown_to_html(coaching_md)
    else:
        body = ('<p class="coach-empty">Coaching report not available for this '
                'match yet.</p>')

    return (
        '<div class="card coach-card">'
        '<div class="coach-head">'
        '<div class="rounds-title" style="margin:0">AI coaching report '
        '<span class="muted" style="text-transform:none;letter-spacing:0">'
        '(pre-generated &mdash; baked into this page)</span></div>'
        '<button class="coach-btn" id="coach-toggle" aria-expanded="false" '
        'aria-controls="coach-body">View coaching report</button>'
        '</div>'
        f'<div class="coach-body" id="coach-body" hidden>{body}</div>'
        '<script>(function(){'
        'var b=document.getElementById("coach-toggle"),'
        'p=document.getElementById("coach-body");'
        'if(!b||!p)return;'
        'b.addEventListener("click",function(){'
        'var hidden=p.hasAttribute("hidden");'
        'if(hidden){p.removeAttribute("hidden");'
        'b.textContent="Hide coaching report";b.setAttribute("aria-expanded","true");}'
        'else{p.setAttribute("hidden","");'
        'b.textContent="View coaching report";b.setAttribute("aria-expanded","false");}'
        '});})();</script>'
        '</div>'
    )


def embed_json(obj) -> str:
    """Serialize for inlining inside a <script> tag.

    json.dumps does not escape '<', so a player name containing '</script>'
    would close the tag early and corrupt the blob. Escaping <, >, & to their
    \\uXXXX form is valid JSON (parses back identically) and is inert to the HTML
    tokenizer.
    """
    return (json.dumps(obj, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def kd_ratio(kills, deaths) -> str:
    if deaths:
        return f"{kills / deaths:.2f}"
    return "∞" if kills else "0.00"   # infinity if kills but no deaths


PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>demoReview</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&family=Rajdhani:wght@500;600;700&display=swap">
<style>
  :root {
    /* Tactical CS2 palette -- the game's own side colors. */
    --bg: #0B0E11; --card: #12161B; --card2: #171C22; --line: #232A31;
    --text: #E4E7EA; --muted: #8A929B;
    --you: #4A90D9; --opp: #DE9B35;          /* CT (your team) / T (opponent) */
    --me: #6B9E6B; --me-death: #C1554A;       /* highlighted player */
    --win: #6B9E6B; --loss: #C1554A;
    --display: "Rajdhani", "Oswald", "Segoe UI", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    /* grenade types (smoke / molotov+incendiary / HE / flash / decoy) */
    --n-smoke: #cfd6df; --n-molotov: #ff7a3c; --n-he: #ffd166;
    --n-flash: #e8f0ff; --n-decoy: #c792ea;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5; padding: 0 16px 40px;
  }
  .wrap { max-width: 880px; margin: 0 auto; }

  /* Condensed HUD type for headers; monospace for every column of numbers so
     they line up like a real scoreboard (functional, not decorative). */
  .brand, .rounds-title, .ym-title, .score-big, .l4-h, h1, h2, h3 { font-family: var(--display); }
  .sscore, .mekd, .rscore, .rtime, .chip .cval, td.num, .l4-row .val,
  .h2h-k, .h2h-d, .econ-player .pmoney, .econ-head, .sb td.rating {
    font-family: var(--mono); font-variant-numeric: tabular-nums; }

  /* Visible keyboard focus across interactive report elements. */
  .dot:focus-visible, .filt:focus-visible, .site-nav a:focus-visible,
  .seg button:focus-visible, .rbtn:focus-visible, .sb th:focus-visible {
    outline: 2px solid var(--you); outline-offset: -2px; }

  /* Slim site nav injected only on the web match page (absent for the CLI).
     Sticky so the "back to My matches" link stays reachable while scrolling. */
  .site-nav { position: sticky; top: 0; z-index: 60;
              display: flex; align-items: center; gap: 14px;
              height: 46px; padding: 0 2px; margin: 0 0 6px;
              background: rgba(11,14,17,.92);
              backdrop-filter: blur(8px); -webkit-backdrop-filter: blur(8px);
              border-bottom: 1px solid var(--line); font-size: .85rem; }
  /* With the web nav present, the report header sticks just below it (not over). */
  .wrap:has(> .site-nav) .sticky { top: 46px; }
  .site-nav .sn-brand { font-family: var(--display); font-weight: 700;
              text-transform: uppercase; letter-spacing: .1em; color: var(--text); }
  .site-nav a { color: var(--you); text-decoration: none; font-weight: 600; }
  .site-nav a:hover { text-decoration: underline; }
  .site-nav .sn-note { color: var(--opp); font-style: italic; font-size: .82rem; }
  .site-nav .sn-user { margin-left: auto; color: var(--muted);
              display: inline-flex; align-items: center; gap: 8px; }
  .site-nav .sn-user img { width: 22px; height: 22px; border-radius: 4px; }

  /* Sticky header (score + jump bar + filters) */
  .sticky {
    position: sticky; top: 0; z-index: 50;
    background: rgba(11,14,17,.86); backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border: 1px solid var(--line); border-top: none;
    border-radius: 0 0 14px 14px; padding: 12px 18px; margin-bottom: 22px;
  }
  .bar { display: flex; align-items: center; gap: 8px 14px; flex-wrap: wrap; }
  .bar + .bar { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--line); }
  .brand { font-weight: 700; font-size: 1.02rem; text-transform: uppercase; letter-spacing: .1em; }
  .sscore { font-size: 1.05rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .sscore .dash { color: var(--muted); margin: 0 5px; }
  .you { color: var(--you); } .opp { color: var(--opp); }
  .pill { font-size: .68rem; font-weight: 700; padding: 2px 9px; border-radius: 999px;
          text-transform: uppercase; letter-spacing: .04em; }
  .pill.win { background: rgba(107,158,107,.16); color: var(--win); }
  .pill.loss { background: rgba(193,85,74,.16); color: var(--loss); }
  .muted { color: var(--muted); }
  .mekd { margin-left: auto; color: var(--muted); font-size: .85rem; }
  .mekd b { color: var(--text); }

  /* Round jump bar as a tactical timeline: flush ticks, thin dividers, round
     number in mono, a bottom outcome bar + up/down marker, CT-blue = current.
     Keeps the .dot / data-round / #round-N / .current contract the JS relies on. */
  .jump-bar { gap: 0; align-items: stretch; flex-wrap: wrap;
              border: 1px solid var(--line); border-radius: 8px;
              background: var(--card2); overflow: hidden; }
  .dot { position: relative; display: inline-flex; flex-direction: column;
         align-items: center; justify-content: center; gap: 1px;
         min-width: 30px; padding: 5px 5px 9px; font-family: var(--mono);
         font-size: .72rem; font-weight: 600; text-decoration: none;
         color: var(--muted); cursor: pointer;
         border-left: 1px solid var(--line); transition: background .1s, color .1s; }
  .dot:first-child { border-left: 0; }
  .dot:hover { background: rgba(74,144,217,.10); color: var(--text); }
  .dot::before { content: "\\00B7"; font-size: .62rem; line-height: 1; color: var(--muted); }
  .dot::after { content: ""; position: absolute; left: 4px; right: 4px; bottom: 3px;
                height: 2px; border-radius: 1px; background: var(--muted); opacity: .45; }
  .dot.green { color: #9dbf9d; }
  .dot.green::before { content: "\\25B2"; color: var(--win); }       /* triangle up = round won */
  .dot.green::after  { background: var(--win); opacity: .9; }
  .dot.red { color: #cf9089; }
  .dot.red::before { content: "\\25BC"; color: var(--loss); }        /* triangle down = round lost */
  .dot.red::after  { background: var(--loss); opacity: .9; }
  .dot.gray::before { content: "\\2013"; }                           /* en dash = no outcome */
  .dot.current { background: rgba(74,144,217,.16); color: var(--text); }
  .dot.current::after { background: var(--you); opacity: 1; height: 3px; }
  @media (prefers-reduced-motion: reduce) { .dot { transition: none; } }

  .filter-bar { gap: 8px; }
  .filt { font-family: inherit; font-size: .8rem; color: var(--muted);
          background: var(--card2); border: 1px solid var(--line);
          padding: 5px 12px; border-radius: 8px; cursor: pointer; }
  .filt:hover { color: var(--text); }
  .filt.active { background: var(--you); color: #0B0E11; border-color: var(--you); font-weight: 600; }

  .sub { color: var(--muted); font-size: .9rem; margin: 4px 0 20px; }
  .name.me { color: var(--me); font-weight: 700; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 20px 22px; margin-bottom: 18px;
  }
  .score-big { font-size: 2rem; font-weight: 700; letter-spacing: -0.02em;
               display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .result { font-size: .8rem; font-weight: 700; padding: 4px 12px; border-radius: 999px;
            text-transform: uppercase; letter-spacing: .04em; }
  .result.win { background: rgba(107,158,107,.16); color: var(--win); }
  .result.loss { background: rgba(193,85,74,.16); color: var(--loss); }
  .stat-row { display: flex; gap: 28px; flex-wrap: wrap; margin-top: 8px; color: var(--muted); font-size: .9rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: .92rem; }
  th { text-align: left; color: var(--muted); font-weight: 600; font-size: .78rem;
       text-transform: uppercase; letter-spacing: .04em; padding: 6px 10px; border-bottom: 1px solid var(--line); }
  td { padding: 7px 10px; border-bottom: 1px solid var(--line); }
  tr:last-child td { border-bottom: none; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .tag { font-size: .68rem; font-weight: 700; padding: 1px 7px; border-radius: 999px; }
  .tag.you { background: rgba(74,144,217,.16); color: var(--you); }
  .tag.opp { background: rgba(222,155,53,.16); color: var(--opp); }
  tr.me td { background: rgba(107,158,107,.12); }
  tr.me td:first-child { border-left: 3px solid var(--me); font-weight: 700; }
  .rounds-title { color: var(--muted); font-size: .8rem; text-transform: uppercase;
                  letter-spacing: .05em; margin: 30px 0 12px; }
  .round { scroll-margin-top: 150px; }
  .round-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
  .round-no { font-weight: 700; font-size: 1.1rem; }
  .badge { font-size: .72rem; font-weight: 700; padding: 3px 9px; border-radius: 999px;
           text-transform: uppercase; letter-spacing: .03em; }
  .badge.win { background: rgba(107,158,107,.18); color: var(--win); }
  .badge.loss { background: rgba(193,85,74,.18); color: var(--loss); }
  .played { color: var(--muted); font-size: .8rem; }
  .reason { color: var(--muted); font-size: .82rem; }
  .rscore { margin-left: auto; font-size: .9rem; font-variant-numeric: tabular-nums; }
  .meta { display: flex; flex-wrap: wrap; gap: 8px 20px; font-size: .82rem;
          color: var(--muted); margin-bottom: 12px; }
  .meta b { color: var(--text); font-weight: 600; }
  .opening-died { color: var(--me-death); font-weight: 700; }

  /* Collapsible per-round economy */
  .econ { margin-bottom: 12px; border: 1px solid var(--line); border-radius: 9px;
          background: var(--card2); }
  .econ summary { cursor: pointer; padding: 8px 12px; font-size: .82rem;
                  color: var(--muted); list-style: none; user-select: none; }
  .econ summary::-webkit-details-marker { display: none; }
  .econ summary::before { content: "▸"; display: inline-block; margin-right: 8px;
                          color: var(--muted); transition: transform .12s; }
  .econ[open] summary::before { transform: rotate(90deg); }
  .econ summary b { color: var(--text); }
  .econ-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
               padding: 4px 12px 12px; }
  @media (max-width: 560px) { .econ-grid { grid-template-columns: 1fr; } }
  .econ-head { font-size: .74rem; font-weight: 700; text-transform: uppercase;
               letter-spacing: .03em; margin-bottom: 6px; padding-bottom: 5px;
               border-bottom: 1px solid var(--line); }
  .econ-head.you { color: var(--you); } .econ-head.opp { color: var(--opp); }
  .econ-player { display: flex; align-items: baseline; gap: 8px; font-size: .82rem;
                 padding: 3px 0; }
  .econ-player.me { color: var(--me); font-weight: 700; }
  .econ-player .pname { flex: 1; }
  .econ-player .ptype { font-size: .68rem; color: var(--muted);
                        text-transform: uppercase; letter-spacing: .03em; }
  .econ-player.me .ptype { color: var(--me); }
  .econ-player .pmoney { color: var(--muted); font-variant-numeric: tabular-nums; font-size: .76rem; }
  .econ-player.me .pmoney { color: var(--me); }
  .kill { display: flex; align-items: center; gap: 8px; padding: 6px 10px;
          border-left: 3px solid transparent; border-radius: 7px; font-size: .92rem; }
  .kill + .kill { margin-top: 2px; }
  .kill.mine-kill { background: rgba(107,158,107,.12); border-left-color: var(--me); }
  .kill.mine-death { background: rgba(193,85,74,.10); border-left-color: var(--me-death); }
  .name { font-weight: 600; }
  .name.ally { color: var(--you); } .name.enemy { color: var(--opp); }
  .name.me { color: var(--me); font-weight: 700; }
  .arrow { color: var(--muted); }
  .wep { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .78rem;
         background: var(--card2); color: #c8cdd6; padding: 2px 7px; border-radius: 5px; }
  .hs { font-size: .64rem; font-weight: 700; color: #10131a; background: #ffd166;
        padding: 1px 5px; border-radius: 4px; }
  .me-tag { font-size: .64rem; font-weight: 700; color: #10131a; padding: 1px 6px; border-radius: 4px; }
  .me-tag.kill { background: var(--me); } .me-tag.death { background: var(--me-death); }
  .open { font-size: .62rem; font-weight: 700; color: #10131a; background: #c792ea;
          padding: 1px 6px; border-radius: 4px; letter-spacing: .03em; }
  .no-kills { color: var(--muted); font-size: .85rem; font-style: italic; padding: 4px 10px; }
  .note { color: var(--muted); font-size: .82rem; margin-top: 12px; }
  .empty { color: var(--muted); font-style: italic; padding: 20px; text-align: center; display: none; }

  /* Sortable pro scoreboard */
  .sb th[data-sort] { cursor: pointer; user-select: none; white-space: nowrap; }
  .sb th[data-sort]:hover { color: var(--text); }
  .sb th.sorted { color: var(--text); }
  .sb th.sorted[data-dir="desc"]::after { content: " \\25be"; }
  .sb th.sorted[data-dir="asc"]::after { content: " \\25b4"; }
  .sb td.rating, .sb th[data-sort="rating"] { font-weight: 700; color: var(--text); }

  /* Your-match stat strip */
  .your-match { margin-top: 16px; }
  .ym-title { font-size: .78rem; text-transform: uppercase; letter-spacing: .04em;
              color: var(--muted); margin-bottom: 8px; }
  .chips { display: flex; flex-wrap: wrap; gap: 8px; }
  .chip { background: var(--card2); border: 1px solid var(--line); border-radius: 9px;
          padding: 6px 11px; display: flex; flex-direction: column; gap: 1px; min-width: 62px; }
  .chip .clabel { font-size: .62rem; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); }
  .chip .cval { font-size: 1rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .chip.hi .cval { color: var(--me); }

  /* Round insight badges */
  .ibadges { display: flex; gap: 6px; flex-wrap: wrap; margin: 0 0 10px; }
  .ibadge { font-size: .66rem; font-weight: 700; padding: 2px 8px; border-radius: 999px; letter-spacing: .02em; }
  .ibadge.clutch-won { background: rgba(107,158,107,.18); color: var(--win); }
  .ibadge.clutch-lost { background: rgba(193,85,74,.14); color: var(--loss); }
  .ibadge.trade { background: rgba(74,144,217,.16); color: var(--you); }
  .ibadge.multi { background: rgba(199,146,234,.18); color: #c792ea; }
  .ibadge.mine { outline: 1px solid var(--me); outline-offset: 1px; }

  /* Map heatmap */
  .map-controls { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
  .seg { display: inline-flex; background: var(--card2); border: 1px solid var(--line);
         border-radius: 9px; overflow: hidden; }
  .seg button { font: inherit; font-size: .78rem; color: var(--muted); background: transparent;
                border: 0; padding: 5px 11px; cursor: pointer; }
  .seg button + button { border-left: 1px solid var(--line); }
  .seg button.active { background: var(--you); color: #0B0E11; font-weight: 600; }
  .map-legend { font-size: .76rem; color: var(--muted); display: flex; align-items: center; gap: 6px; }
  .map-legend i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .map-legend .lk { background: var(--me); } .map-legend .ld { background: var(--me-death); }
  .mapwrap, .replaywrap { position: relative; width: 100%; max-width: 560px; margin: 0 auto; aspect-ratio: 1 / 1; }
  .radar-bg { position: absolute; inset: 0; border-radius: 10px; border: 1px solid var(--line); }
  .mapwrap svg, .replaywrap canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
  .mapwrap circle.d { display: none; }
  .mapwrap circle.kill { fill: var(--me); fill-opacity: .72; }
  .mapwrap circle.death { fill: var(--me-death); fill-opacity: .72; }
  .mapwrap circle.me { stroke: #fff; stroke-width: 2; fill-opacity: .95; }
  .mapwrap circle.nade { fill-opacity: .8; stroke: rgba(0,0,0,.35); stroke-width: .5; }
  .mapwrap circle.smoke   { fill: var(--n-smoke); }
  .mapwrap circle.molotov { fill: var(--n-molotov); }
  .mapwrap circle.he      { fill: var(--n-he); }
  .mapwrap circle.flash   { fill: var(--n-flash); }
  .mapwrap circle.decoy   { fill: var(--n-decoy); }
  .mapwrap line.nade { display: none; stroke-width: 1.5; stroke-opacity: .4; }
  .mapwrap line.smoke { stroke: var(--n-smoke); } .mapwrap line.molotov { stroke: var(--n-molotov); }
  .mapwrap line.he { stroke: var(--n-he); } .mapwrap line.flash { stroke: var(--n-flash); }
  .mapwrap line.decoy { stroke: var(--n-decoy); }
  .nade-legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: .74rem;
                 color: var(--muted); margin-top: 8px; align-items: center; }
  .nade-legend i { width: 9px; height: 9px; border-radius: 50%; display: inline-block;
                   margin-right: 4px; vertical-align: -1px; }
  .nl-smoke { background: var(--n-smoke); } .nl-molotov { background: var(--n-molotov); }
  .nl-he { background: var(--n-he); } .nl-flash { background: var(--n-flash); }
  .nl-decoy { background: var(--n-decoy); }

  /* Round replay */
  .replay-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
  .rbtn { font: inherit; font-size: .82rem; color: var(--text); background: var(--card2);
          border: 1px solid var(--line); border-radius: 8px; padding: 5px 10px; cursor: pointer; min-width: 34px; }
  .rbtn:hover { border-color: var(--you); }
  .rbtn.play { background: var(--you); color: #0B0E11; font-weight: 700; }
  .rsel { font: inherit; font-size: .82rem; color: var(--text); background: var(--card2);
          border: 1px solid var(--line); border-radius: 8px; padding: 5px 8px; }
  .rtime { font-variant-numeric: tabular-nums; color: var(--muted); font-size: .82rem; min-width: 44px; }
  .revt { color: var(--muted); font-size: .8rem; margin-left: auto; }
  .rscrub { width: 100%; max-width: 560px; margin: 0 auto 10px; display: block; accent-color: var(--you); }

  /* Replay killfeed overlay (stacks newest-on-top, top-right of the radar) */
  .replaywrap { }
  .killfeed { position: absolute; top: 8px; right: 8px; display: flex;
              flex-direction: column; gap: 3px; align-items: flex-end;
              pointer-events: none; z-index: 2; }
  .killfeed .kf-n.pn { pointer-events: auto; cursor: pointer; }
  .kf-row { display: flex; align-items: center; gap: 5px; font-size: .78rem;
            font-weight: 600; background: rgba(11,14,17,.72); border: 1px solid var(--line);
            border-radius: 6px; padding: 2px 7px; white-space: nowrap;
            backdrop-filter: blur(3px); -webkit-backdrop-filter: blur(3px); }
  .kf-row.fresh { outline: 1px solid rgba(255,255,255,.25); }
  .kf-n.you { color: var(--me); } .kf-n.ally { color: var(--you); }
  .kf-n.enemy { color: var(--opp); } .kf-n { color: var(--text); }
  .kf-x { color: var(--me-death); font-weight: 700; }
  .kf-hs { font-size: .6rem; font-weight: 700; color: #10131a; background: #ffd166;
           padding: 0 4px; border-radius: 3px; margin-left: 2px; }

  /* Clickable player names (focus switch) */
  .pn { cursor: pointer; }
  .pn:hover { text-decoration: underline; text-underline-offset: 2px; }
  tr.me td .pn { font-weight: 700; }

  /* Layer 5 rule badges on kill lines */
  .rbadge { font-size: .6rem; font-weight: 700; padding: 1px 6px; border-radius: 4px;
            margin-left: 6px; letter-spacing: .02em; text-transform: uppercase; white-space: nowrap; }
  .rbadge.risky { background: rgba(222,155,53,.18); color: var(--opp);
                  outline: 1px solid rgba(222,155,53,.5); }
  .rbadge.attempt { background: rgba(199,146,234,.2); color: #c792ea; }
  .rbadge.clutch { background: rgba(107,158,107,.2); color: var(--me); }

  /* Layer 4 breakdown charts */
  #layer4 { padding: 18px 20px; }
  .l4-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 22px 26px; }
  @media (max-width: 620px) { .l4-grid { grid-template-columns: 1fr; } }
  .l4-block { min-width: 0; }
  .l4-h { font-size: .8rem; font-weight: 700; margin: 0 0 10px; color: var(--text); }
  .l4-h .sub2 { color: var(--muted); font-weight: 400; font-size: .78rem; }
  .l4-row { display: grid; grid-template-columns: 92px 1fr auto; align-items: center;
            gap: 8px; font-size: .8rem; margin-bottom: 6px; }
  .l4-row .lbl { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .l4-row .val { font-variant-numeric: tabular-nums; color: var(--text); font-weight: 600; white-space: nowrap; }
  .l4-track { position: relative; height: 14px; background: var(--card2);
              border-radius: 4px; overflow: hidden; }
  .l4-fill { position: absolute; inset: 0 auto 0 0; height: 100%; border-radius: 4px; }
  .l4-fill.win { background: var(--win); } .l4-fill.loss { background: var(--loss); }
  .l4-fill.you { background: var(--you); } .l4-fill.me { background: var(--me); }
  .l4-fill.opp { background: var(--opp); } .l4-fill.mut { background: var(--muted); }
  .l4-split { display: flex; height: 14px; border-radius: 4px; overflow: hidden; background: var(--card2); }
  .l4-split i { display: block; height: 100%; }
  .l4-split i.w { background: var(--win); } .l4-split i.l { background: var(--loss); }
  .l4-empty { color: var(--muted); font-style: italic; font-size: .82rem; }
  .l4-note { color: var(--muted); font-size: .76rem; margin-top: 14px; }
  .h2h-k { color: var(--me); } .h2h-d { color: var(--me-death); }
  .l4-row.worst .lbl { color: var(--opp); font-weight: 700; }

  /* Layer 6 -- AI coaching report card */
  .coach-head { display: flex; align-items: center; justify-content: space-between;
                gap: 12px; flex-wrap: wrap; }
  .coach-btn { font: inherit; font-size: .85rem; font-weight: 600; color: #0B0E11;
               background: var(--me); border: 0; border-radius: 9px; padding: 8px 16px;
               cursor: pointer; white-space: nowrap; transition: filter .1s; }
  .coach-btn:hover { filter: brightness(1.08); }
  .coach-body { margin-top: 16px; border-top: 1px solid var(--line); padding-top: 14px;
                font-size: .92rem; }
  .coach-body h3 { font-size: 1rem; margin: 18px 0 8px; color: var(--me); }
  .coach-body h4 { font-size: .9rem; margin: 14px 0 6px; color: var(--text); }
  .coach-body h5 { font-size: .84rem; margin: 12px 0 6px; color: var(--muted); }
  .coach-body > :first-child { margin-top: 0; }
  .coach-body p { margin: 8px 0; }
  .coach-body ul, .coach-body ol { margin: 8px 0; padding-left: 22px; }
  .coach-body li { margin: 4px 0; }
  .coach-body strong { color: var(--text); font-weight: 700; }
  .coach-body code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                     font-size: .82em; background: var(--card2); padding: 1px 5px;
                     border-radius: 4px; }
  .coach-empty { color: var(--muted); font-style: italic; }
</style>
</head>
<body>
<div class="wrap">
"""

PAGE_SCRIPT = """<script>
(function () {
  // ===== Focus-player controller: click any name to re-spotlight, no reload =====
  var FD = {};
  try { FD = JSON.parse(document.getElementById('focus-data').textContent); } catch (e) {}
  var PROFILES = FD.profiles || {}, ROSTER = FD.roster || [], RULES = FD.rules || {};
  var focusSid = FD.focus;
  var teamA = {}, pname = {};
  ROSTER.forEach(function (p) { teamA[p.sid] = p.teamA; pname[p.sid] = p.name; });
  var redrawReplay = null, resetFeed = null, refreshMapFilter = null;

  function he(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function roleForSid(sid) {                       // 'you' / 'ally' / 'enemy' / ''
    if (!sid) return '';
    if (sid === focusSid) return 'you';
    var t = teamA[sid];
    if (t === undefined) return 'enemy';
    return t === teamA[focusSid] ? 'ally' : 'enemy';
  }

  function applySubheader() {
    var el = document.getElementById('sub-focus');
    if (el) el.textContent = pname[focusSid] || '?';
  }
  function applyScoreboard() {
    var sb = document.getElementById('scoreboard');
    if (!sb) return;
    var ft = teamA[focusSid];
    Array.prototype.forEach.call(sb.querySelectorAll('tbody tr'), function (tr) {
      var sid = tr.dataset.sid;
      tr.classList.toggle('me', sid === focusSid);
      var cell = tr.querySelector('.teamtag');
      if (cell) cell.innerHTML = (teamA[sid] === ft)
        ? '<span class="tag you">You</span>' : '<span class="tag opp">Opp</span>';
    });
  }
  function buildYourMatch() {
    var host = document.getElementById('ym-chips');
    var p = PROFILES[focusSid];
    if (!host || !p) return;
    var s = p.stats;
    var chips = [
      ['Rating', s.rating.toFixed(2), 1], ['ADR', Math.round(s.adr), 1],
      ['KAST', Math.round(s.kast) + '%', 1],
      ['K / D / A', s.kills + ' / ' + s.deaths + ' / ' + s.assists, 0],
      ['HS%', Math.round(s.hs_pct) + '%', 0],
      ['Opening', s.opening_k + '-' + s.opening_d + ' (' + Math.round(s.open_pct) + '%)', 0],
      ['Multi-kills', s.multi, 0], ['Trade kills', s.trade_kills, 0],
      ['Clutches', s.clutch_won + '/' + s.clutch, 0], ['Flash assists', s.flash_assists, 0],
    ];
    host.innerHTML = chips.map(function (c) {
      return '<div class="chip' + (c[2] ? ' hi' : '') + '"><span class="clabel">'
        + c[0] + '</span><span class="cval">' + he(c[1]) + '</span></div>';
    }).join('');
    var nm = document.getElementById('ym-name');
    if (nm) nm.textContent = pname[focusSid] || '?';
  }

  // ---- Layer 4 breakdown charts (plain divs/CSS bars, no libraries) ----
  function l4block(h, inner) {
    return '<div class="l4-block"><div class="l4-h">' + h + '</div>' + inner + '</div>';
  }
  function l4row(label, widthPct, cls, val, extra) {
    return '<div class="l4-row' + (extra || '') + '"><span class="lbl">' + he(String(label))
      + '</span><span class="l4-track"><span class="l4-fill ' + cls + '" style="width:'
      + widthPct + '%"></span></span><span class="val">' + val + '</span></div>';
  }
  function winByBuyBlock(wbb) {
    var out = '';
    ['CT', 'T'].forEach(function (side) {
      ['eco', 'force', 'full'].forEach(function (b) {
        var w = wbb[side][b][0], l = wbb[side][b][1], t = w + l;
        if (!t) return;
        out += '<div class="l4-row"><span class="lbl">' + side + ' ' + b + '</span>'
          + '<span class="l4-split"><i class="w" style="width:' + (100 * w / t)
          + '%"></i><i class="l" style="width:' + (100 * l / t) + '%"></i></span>'
          + '<span class="val">' + w + '-' + l + '</span></div>';
      });
    });
    if (!out) out = '<div class="l4-empty">no data</div>';
    return l4block('Win rate by buy type <span class="sub2">(W-L)</span>', out);
  }
  function deathTypeBlock(dbt) {
    var total = dbt.eco + dbt.force + dbt.full;
    if (!total) return l4block('Deaths by round buy', '<div class="l4-empty">no deaths</div>');
    var max = Math.max(1, dbt.eco, dbt.force, dbt.full);
    var rows = [['eco', dbt.eco], ['force', dbt.force], ['full', dbt.full]].map(function (p) {
      return l4row(p[0], 100 * p[1] / max, 'loss', p[1]);
    }).join('');
    return l4block('Deaths by round buy <span class="sub2">(your team buy)</span>', rows);
  }
  function deathWeaponBlock(dbw) {
    if (!dbw.length) return l4block('Deaths by enemy weapon', '<div class="l4-empty">no deaths</div>');
    var top = dbw.slice(0, 8), max = Math.max.apply(null, top.map(function (x) { return x[1]; }));
    var rows = top.map(function (x) { return l4row(x[0], 100 * x[1] / max, 'opp', x[1]); }).join('');
    return l4block('Deaths by enemy weapon', rows);
  }
  function h2hBlock(list) {
    if (!list.length) return l4block('Head-to-head', '<div class="l4-empty">no duels</div>');
    var rows = list.map(function (x, i) {
      var t = x.k + x.d || 1;
      return '<div class="l4-row' + (i === 0 ? ' worst' : '') + '"><span class="lbl">'
        + he(x.name) + '</span><span class="l4-split"><i class="w" style="width:'
        + (100 * x.k / t) + '%"></i><i class="l" style="width:' + (100 * x.d / t)
        + '%"></i></span><span class="val"><span class="h2h-k">K ' + x.k
        + '</span> / <span class="h2h-d">D ' + x.d + '</span></span></div>';
    }).join('');
    return l4block('Head-to-head <span class="sub2">(worst matchup first)</span>', rows);
  }
  function buildLayer4() {
    var host = document.getElementById('layer4');
    var p = PROFILES[focusSid];
    if (!host || !p) return;
    var blocks = [winByBuyBlock(p.winByBuy), h2hBlock(p.h2h),
                  deathTypeBlock(p.deathsByType), deathWeaponBlock(p.deathsByWeapon)];
    var nm = he(pname[focusSid] || '?');
    host.innerHTML = '<div class="l4-grid">' + blocks.join('') + '</div>'
      + '<div class="l4-note">Win rate = ' + nm
      + "'s team, by buy type and side. Deaths and head-to-head are "
      + nm + "'s own; head-to-head bar shows kills (green) vs deaths (red).</div>";
  }

  // ---- Per-kill line: focus highlight, name colours, FOCUS KILL/DEATH tag ----
  function applyKillLines() {
    Array.prototype.forEach.call(document.querySelectorAll('.kill[data-round]'), function (el) {
      var ka = el.dataset.ka, kv = el.dataset.kv;
      var isK = ka && ka === focusSid, isD = !isK && kv && kv === focusSid;
      el.classList.toggle('mine-kill', !!isK);
      el.classList.toggle('mine-death', !!isD);
      Array.prototype.forEach.call(el.querySelectorAll('.name[data-sid]'), function (n) {
        var sid = n.dataset.sid;
        n.className = 'name pn ' + (sid === focusSid ? 'me' : roleForSid(sid));
      });
      var old = el.querySelector('.me-tag');
      if (old) old.remove();
      var slot = el.querySelector('.rbadge-slot');
      if (isK || isD) {
        var tag = document.createElement('span');
        tag.className = 'me-tag ' + (isK ? 'kill' : 'death');
        tag.textContent = isK ? 'FOCUS KILL' : 'FOCUS DEATH';
        if (slot) el.insertBefore(tag, slot); else el.appendChild(tag);
      }
    });
  }
  // ---- Layer 5 rule badges (attached to the relevant kill line, per focus) ----
  function applyRuleBadges() {
    Array.prototype.forEach.call(document.querySelectorAll('.rbadge-slot'), function (s) {
      s.innerHTML = '';
    });
    Object.keys(RULES).forEach(function (r) {
      var fl = RULES[r][focusSid];
      if (!fl) return;
      fl.forEach(function (f) {
        var line = document.querySelector('.kill[data-round="' + r + '"][data-idx="' + f.line + '"]');
        var slot = line && line.querySelector('.rbadge-slot');
        if (!slot) return;
        if (f.kind === 'risky-duel')
          slot.innerHTML = '<span class="rbadge risky" title="Died first while teammates were alive at a disadvantage">RISKY DUEL</span>';
        else if (f.kind === 'clutch-won')
          slot.innerHTML = '<span class="rbadge clutch" title="Won the round as the lone survivor">CLUTCH WON 1v' + f.x + '</span>';
        else
          slot.innerHTML = '<span class="rbadge attempt" title="Left as the last player alive against 2+ enemies">CLUTCH ATTEMPT 1v' + f.x + '</span>';
      });
    });
  }
  function applyMapDots() {
    var mapwrap = document.getElementById('mapwrap');
    if (!mapwrap) return;
    Array.prototype.forEach.call(mapwrap.querySelectorAll('.d[data-sid]'), function (c) {
      var me = c.dataset.sid === focusSid;
      c.dataset.me = me ? '1' : '0';
      c.classList.toggle('me', me);
    });
    if (refreshMapFilter) refreshMapFilter();
  }

  function applyFocus() {
    applySubheader(); applyScoreboard(); buildYourMatch(); buildLayer4();
    applyKillLines(); applyRuleBadges(); applyMapDots();
    if (resetFeed) resetFeed();
    if (redrawReplay) redrawReplay();
  }
  function refocus(sid) {
    if (!sid || !(sid in PROFILES) || sid === focusSid) return;
    focusSid = sid;
    applyFocus();
  }
  document.addEventListener('click', function (e) {
    var el = e.target.closest ? e.target.closest('.pn') : null;
    if (el && el.dataset.sid) refocus(el.dataset.sid);
  });
  // ===== end focus controller (initial applyFocus() runs at the very bottom) =====

  var sticky = document.querySelector('.sticky');
  var cards = Array.prototype.slice.call(document.querySelectorAll('.round'));
  var dots = Array.prototype.slice.call(document.querySelectorAll('.dot'));
  var filters = Array.prototype.slice.call(document.querySelectorAll('.filt'));
  var empty = document.querySelector('.empty');

  function matches(card, kind) {
    var mk = +card.dataset.mk, md = +card.dataset.md, rv = card.dataset.rival === '1';
    if (kind === 'deaths') return md > 0;
    if (kind === 'multi') return mk >= 2;
    if (kind === 'rival') return rv;
    if (kind === 'clutch') return card.dataset.clutch === '1';
    return true;
  }
  function setFilter(kind) {
    var shown = 0;
    cards.forEach(function (c) {
      var ok = matches(c, kind);
      c.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    filters.forEach(function (b) { b.classList.toggle('active', b.dataset.filter === kind); });
    if (empty) empty.style.display = shown ? 'none' : 'block';
  }
  filters.forEach(function (b) {
    b.addEventListener('click', function () { setFilter(b.dataset.filter); });
  });

  function scrollToRound(n) {
    var el = document.getElementById('round-' + n);
    if (!el) return;
    if (getComputedStyle(el).display === 'none') setFilter('all');
    var off = sticky.offsetHeight + 12;
    var y = el.getBoundingClientRect().top + window.pageYOffset - off;
    window.scrollTo({ top: y, behavior: 'smooth' });
  }
  dots.forEach(function (d) {
    d.addEventListener('click', function (e) { e.preventDefault(); scrollToRound(d.dataset.round); });
  });

  // Sortable pro scoreboard.
  var sb = document.getElementById('scoreboard');
  if (sb) {
    var tbody = sb.querySelector('tbody');
    var headers = Array.prototype.slice.call(sb.querySelectorAll('th[data-sort]'));
    var curKey = 'rating', curDir = -1;   // start: rating, descending
    function sortBy(key) {
      if (key === curKey) { curDir = -curDir; }
      else { curKey = key; curDir = (key === 'name') ? 1 : -1; }
      var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
      rows.sort(function (a, b) {
        var av = a.dataset[key], bv = b.dataset[key];
        if (key === 'name') return curDir * av.localeCompare(bv);
        return curDir * (parseFloat(av) - parseFloat(bv));
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
      headers.forEach(function (h) {
        var on = h.dataset.sort === curKey;
        h.classList.toggle('sorted', on);
        if (on) h.setAttribute('data-dir', curDir < 0 ? 'desc' : 'asc');
        else h.removeAttribute('data-dir');
      });
    }
    headers.forEach(function (h) {
      h.addEventListener('click', function () { sortBy(h.dataset.sort); });
    });
  }

  // Map heatmap: toggle scope (you / everyone) and type (kills / deaths / both).
  var mapwrap = document.getElementById('mapwrap');
  if (mapwrap) {
    var els = Array.prototype.slice.call(mapwrap.querySelectorAll('.d'));
    var mapScope = 'you', mapType = 'both', mapNades = 'off';
    function updateMap() {
      els.forEach(function (c) {
        var dt = c.dataset.type;
        if (dt === 'nade') {                       // grenades: own on/off toggle
          c.style.display = (mapNades === 'on') ? 'block' : 'none';
          return;
        }
        var scopeOk = (mapScope === 'all') || c.dataset.me === '1';
        var typeOk = (mapType === 'both')
          || (mapType === 'kills' && dt === 'kill')
          || (mapType === 'deaths' && dt === 'death');
        c.style.display = (scopeOk && typeOk) ? 'block' : 'none';
      });
    }
    document.querySelectorAll('.mapcard .seg').forEach(function (seg) {
      seg.addEventListener('click', function (e) {
        if (e.target.tagName !== 'BUTTON') return;
        seg.querySelectorAll('button').forEach(function (b) {
          b.classList.toggle('active', b === e.target);
        });
        var grp = seg.dataset.group;
        if (grp === 'scope') mapScope = e.target.dataset.val;
        else if (grp === 'nades') mapNades = e.target.dataset.val;
        else mapType = e.target.dataset.val;
        updateMap();
      });
    });
    refreshMapFilter = updateMap;      // let the focus controller re-run the filter
    updateMap();
  }

  // 2D round replay.
  var replayEl = document.getElementById('replay-data');
  if (replayEl) {
    var RP = JSON.parse(replayEl.textContent);
    var cv = document.getElementById('replay-canvas');
    var ctx = cv.getContext('2d');
    var COLORS = { you: '#6B9E6B', ally: '#4A90D9', enemy: '#DE9B35' };
    var NADE_COLORS = { smoke: '#cfd6df', molotov: '#ff7a3c', he: '#ffd166',
                        flash: '#e8f0ff', decoy: '#c792ea' };
    var WEAPONS = RP.weapons || [];                    // registry: index -> tag
    var WALPHA = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';
    function weaponTag(p, f) {                          // held-weapon tag or ''
      if (!p.w) return '';
      var ch = p.w.charAt(f);
      if (ch === '.' || ch === '') return '';
      var i = WALPHA.indexOf(ch);
      return (i >= 0 && i < WEAPONS.length) ? WEAPONS[i] : '';
    }
    var showNades = true;
    var sel = document.getElementById('replay-round');
    var scrub = document.getElementById('replay-scrub');
    var playBtn = document.getElementById('replay-play');
    var speedBtn = document.getElementById('replay-speed');
    var timeLbl = document.getElementById('replay-time');
    var feedEl = document.getElementById('replay-feed');
    var round = RP.rounds[0], cur = 0, playing = false, speed = 2, last = 0, feedSig = '';

    function fps() { return 64 / round.step; }          // sample frames per second
    function esc(s) {
      return String(s).replace(/[&<>"]/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
      });
    }

    function posAt(p, c) {
      var f0 = Math.floor(c), f1 = Math.min(f0 + 1, round.nf - 1), t = c - f0;
      var a = f0 * 3, b = f1 * 3;
      if (p.f[a] === -1) return null;                   // dead at base frame
      if (p.f[b] === -1) return { x: p.f[a], y: p.f[a + 1], yaw: p.f[a + 2] };
      var dy = ((p.f[b + 2] - p.f[a + 2] + 540) % 360) - 180;
      return { x: p.f[a] + (p.f[b] - p.f[a]) * t,
               y: p.f[a + 1] + (p.f[b + 1] - p.f[a + 1]) * t,
               yaw: p.f[a + 2] + dy * t };
    }
    // Head of a grenade's flight, interpolated along its throw arc.
    function nadeAt(g, c) {
      var n = g.trail.length;
      if (!n) return g.det;
      var span = g.df - g.tf;
      var p = span > 0 ? (c - g.tf) / span : 1;
      if (p < 0) p = 0; if (p > 1) p = 1;
      var fi = p * (n - 1), i0 = Math.floor(fi), i1 = Math.min(i0 + 1, n - 1), t = fi - i0;
      return [g.trail[i0][0] + (g.trail[i1][0] - g.trail[i0][0]) * t,
              g.trail[i0][1] + (g.trail[i1][1] - g.trail[i0][1]) * t];
    }
    function drawNade(g) {
      var col = NADE_COLORS[g.t] || '#fff';
      if (cur >= g.tf && cur < g.df) {                 // in flight: arc + head
        ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.globalAlpha = 0.85;
        ctx.beginPath();
        if (g.trail.length) {
          ctx.moveTo(g.trail[0][0], g.trail[0][1]);
          var head = nadeAt(g, cur);
          var upto = Math.floor((g.trail.length - 1)
            * (g.df > g.tf ? (cur - g.tf) / (g.df - g.tf) : 1));
          for (var i = 1; i <= upto && i < g.trail.length; i++)
            ctx.lineTo(g.trail[i][0], g.trail[i][1]);
          ctx.lineTo(head[0], head[1]); ctx.stroke();
          ctx.globalAlpha = 1;
          ctx.beginPath(); ctx.arc(head[0], head[1], 4, 0, 6.2832);
          ctx.fillStyle = col; ctx.fill();
        }
        ctx.globalAlpha = 1;
        return;
      }
      if (cur < g.df || cur >= g.ef) return;            // detonation effect window
      var life = (g.ef > g.df) ? (cur - g.df) / (g.ef - g.df) : 0;   // 0..1
      var x = g.det[0], y = g.det[1];
      if (g.t === 'smoke') {
        ctx.globalAlpha = 0.22; ctx.fillStyle = col;
        ctx.beginPath(); ctx.arc(x, y, 27, 0, 6.2832); ctx.fill();
        ctx.globalAlpha = 0.55; ctx.lineWidth = 1.5; ctx.strokeStyle = col; ctx.stroke();
      } else if (g.t === 'molotov') {
        ctx.globalAlpha = 0.30; ctx.fillStyle = col;
        ctx.beginPath(); ctx.arc(x, y, 22, 0, 6.2832); ctx.fill();
        ctx.globalAlpha = 0.75; ctx.strokeStyle = col; ctx.lineWidth = 2;
        for (var k = 0; k < 7; k++) {                  // flicker tongues (no RNG)
          var a = k * 0.9 + cur * 0.25, rr = 15 + 7 * Math.sin(cur * 0.5 + k);
          ctx.beginPath(); ctx.moveTo(x, y);
          ctx.lineTo(x + Math.cos(a) * rr, y + Math.sin(a) * rr); ctx.stroke();
        }
      } else if (g.t === 'flash') {
        ctx.globalAlpha = Math.max(0, 1 - life); ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(x, y, 6 + life * 42, 0, 6.2832); ctx.stroke();
      } else if (g.t === 'he') {
        ctx.globalAlpha = Math.max(0, 1 - life); ctx.strokeStyle = col; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(x, y, 5 + life * 26, 0, 6.2832); ctx.stroke();
        ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x, y, 4, 0, 6.2832); ctx.fill();
      } else {                                          // decoy: small pulsing dot
        ctx.globalAlpha = 0.8; ctx.fillStyle = col;
        ctx.beginPath(); ctx.arc(x, y, 4 + 2 * Math.sin(cur * 0.4), 0, 6.2832); ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    // A red cross + small name wherever a player has died so far this round.
    function drawDeaths(f) {
      ctx.textAlign = 'center'; ctx.textBaseline = 'top';
      ctx.font = '600 17px -apple-system, sans-serif';
      round.events.forEach(function (e) {
        if (e.f > f || !e.d) return;
        var x = e.d[0], y = e.d[1];
        ctx.globalAlpha = 0.92; ctx.strokeStyle = '#ff3b4e'; ctx.lineWidth = 3.5;
        ctx.beginPath();
        ctx.moveTo(x - 7, y - 7); ctx.lineTo(x + 7, y + 7);
        ctx.moveTo(x + 7, y - 7); ctx.lineTo(x - 7, y + 7); ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.lineWidth = 3; ctx.strokeStyle = 'rgba(0,0,0,.65)';
        ctx.strokeText(e.v, x, y + 9);
        ctx.fillStyle = 'rgba(255,140,148,.95)';
        ctx.fillText(e.v, x, y + 9);
      });
    }

    // Killfeed: newest on top, a few recent kills, rebuilt only when it changes.
    function renderFeed(f) {
      var vis = 0, recent = [];
      for (var i = 0; i < round.events.length; i++) {
        if (round.events[i].f <= f) { vis++; recent.push(round.events[i]); }
      }
      var sig = round.n + ':' + vis;
      if (sig === feedSig) return;
      feedSig = sig;
      recent = recent.slice(-6).reverse();               // newest first, cap 6
      feedEl.innerHTML = recent.map(function (e, idx) {
        var hs = e.hs ? '<span class="kf-hs">HS</span>' : '';
        var ka = e.ks ? ' pn" data-sid="' + esc(e.ks) + '"' : '"';   // clickable if identified
        var va = e.vs ? ' pn" data-sid="' + esc(e.vs) + '"' : '"';
        return '<div class="kf-row' + (idx === 0 ? ' fresh' : '') + '">'
          + '<span class="kf-n ' + roleForSid(e.ks) + ka + '>' + esc(e.k) + '</span>'
          + '<span class="kf-x">&#10006;</span>'
          + '<span class="kf-n ' + roleForSid(e.vs) + va + '>' + esc(e.v) + '</span>' + hs + '</div>';
      }).join('');
    }

    function draw() {
      ctx.clearRect(0, 0, cv.width, cv.height);
      var f = Math.floor(cur);
      if (showNades && round.grenades)
        round.grenades.forEach(drawNade);              // under everything
      drawDeaths(f);                                    // death crosses under live players
      ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
      round.players.forEach(function (p) {
        var pos = posAt(p, cur);
        if (!pos) return;
        var role = roleForSid(p.sid) || p.r;            // live: follows the focus
        var isYou = role === 'you';
        var col = COLORS[role] || '#aaa';
        var yr = pos.yaw * Math.PI / 180;               // yaw 0=east; radar y is flipped
        var rad = isYou ? 15 : 12;
        // Facing line (drawn under the body).
        ctx.strokeStyle = col; ctx.lineWidth = 4;
        ctx.beginPath(); ctx.moveTo(pos.x, pos.y);
        ctx.lineTo(pos.x + Math.cos(yr) * 26, pos.y - Math.sin(yr) * 26); ctx.stroke();
        // Body = an HP "water tank": a faint full-circle tint for the empty part,
        // then the same colour filled from the BOTTOM up to the health level
        // (clipped to the circle), then a coloured outline ring.
        var hpFrac = 1;
        if (p.hp) { var hi = WALPHA.indexOf(p.hp.charAt(f)); if (hi >= 0) hpFrac = hi / 61; }
        ctx.beginPath(); ctx.arc(pos.x, pos.y, rad, 0, 6.2832);
        ctx.globalAlpha = 0.16; ctx.fillStyle = col; ctx.fill();      // empty-tank tint
        ctx.globalAlpha = 1;
        if (hpFrac > 0) {                                             // water fill
          ctx.save();
          ctx.beginPath(); ctx.arc(pos.x, pos.y, rad, 0, 6.2832); ctx.clip();
          var surface = pos.y + rad - 2 * rad * hpFrac;              // y of the surface
          ctx.globalAlpha = 0.9; ctx.fillStyle = col;
          ctx.fillRect(pos.x - rad, surface, 2 * rad, (pos.y + rad) - surface);
          ctx.restore(); ctx.globalAlpha = 1;
        }
        // Blinded? Wash the whole dot toward white in proportion to flash (0-9).
        var bl = p.bl ? +p.bl.charAt(f) : 0;
        if (bl > 0) {
          ctx.save();
          ctx.beginPath(); ctx.arc(pos.x, pos.y, rad, 0, 6.2832); ctx.clip();
          ctx.globalAlpha = (bl / 9) * 0.8; ctx.fillStyle = '#ffffff';
          ctx.fillRect(pos.x - rad, pos.y - rad, 2 * rad, 2 * rad);
          ctx.restore(); ctx.globalAlpha = 1;
        }
        // Coloured outline; the focus player also gets a white ring just outside.
        ctx.lineWidth = 2.5; ctx.strokeStyle = col;
        ctx.beginPath(); ctx.arc(pos.x, pos.y, rad, 0, 6.2832); ctx.stroke();
        if (isYou) {
          ctx.lineWidth = 3; ctx.strokeStyle = '#fff';
          ctx.beginPath(); ctx.arc(pos.x, pos.y, rad + 1.5, 0, 6.2832); ctx.stroke();
        }
        // Name tag above the dot.
        ctx.textBaseline = 'bottom';
        ctx.font = '600 18px -apple-system, sans-serif';
        ctx.lineWidth = 3.5; ctx.strokeStyle = 'rgba(0,0,0,.7)';
        ctx.strokeText(p.n, pos.x, pos.y - rad - 4);
        ctx.fillStyle = col; ctx.fillText(p.n, pos.x, pos.y - rad - 4);
        // Held weapon tag just below the dot — the real in-game gun.
        var wtag = weaponTag(p, f);
        if (wtag) {
          ctx.textBaseline = 'top';
          ctx.font = '700 15px -apple-system, sans-serif';
          ctx.lineWidth = 3; ctx.strokeStyle = 'rgba(0,0,0,.72)';
          ctx.strokeText(wtag, pos.x, pos.y + rad + 3);
          ctx.fillStyle = 'rgba(255,255,255,.92)';
          ctx.fillText(wtag, pos.x, pos.y + rad + 3);
        }
      });
      if (round.bomb) drawBomb(f);
      scrub.value = f;
      timeLbl.textContent = (f / fps()).toFixed(1) + 's';
      renderFeed(f);
    }
    // Planted bomb: a bomb marker + a 40s countdown above it, from the plant frame.
    function drawBomb(f) {
      var b = round.bomb;
      if (f < b.pf) return;                            // not planted yet this frame
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle'; ctx.font = '20px -apple-system, sans-serif';
      ctx.fillText('💣', b.x, b.y);
      var remain = Math.max(0, 40 - (f - b.pf) / fps());
      var txt = remain.toFixed(1) + 's';
      ctx.textBaseline = 'bottom'; ctx.font = '700 18px -apple-system, sans-serif';
      ctx.lineWidth = 3.5; ctx.strokeStyle = 'rgba(0,0,0,.72)';
      ctx.strokeText(txt, b.x, b.y - 15);
      ctx.fillStyle = remain > 10 ? '#ffd166' : '#ff3b4e';
      ctx.fillText(txt, b.x, b.y - 15);
    }
    function loadRound(i) {
      round = RP.rounds[i]; cur = 0; playing = false; playBtn.innerHTML = '&#9654;';
      scrub.max = round.nf - 1; scrub.value = 0; sel.value = i; feedSig = ''; draw();
    }
    function step(ts) {
      if (!playing) return;
      if (!last) last = ts;
      cur += ((ts - last) / 1000) * fps() * speed; last = ts;
      if (cur >= round.nf - 1) { cur = round.nf - 1; playing = false; playBtn.innerHTML = '&#9654;'; }
      draw();
      if (playing) requestAnimationFrame(step);
    }
    playBtn.addEventListener('click', function () {
      playing = !playing;
      if (playing) {
        if (cur >= round.nf - 1) cur = 0;
        last = 0; playBtn.innerHTML = '&#10074;&#10074;'; requestAnimationFrame(step);
      } else { playBtn.innerHTML = '&#9654;'; }
    });
    scrub.addEventListener('input', function () {
      playing = false; playBtn.innerHTML = '&#9654;'; cur = +scrub.value; draw();
    });
    sel.addEventListener('change', function () { loadRound(+sel.value); });
    document.getElementById('replay-prevround').addEventListener('click', function () {
      loadRound(Math.max(0, sel.selectedIndex - 1));
    });
    document.getElementById('replay-nextround').addEventListener('click', function () {
      loadRound(Math.min(RP.rounds.length - 1, sel.selectedIndex + 1));
    });
    document.getElementById('replay-nextkill').addEventListener('click', function () {
      var f = Math.floor(cur);
      for (var i = 0; i < round.events.length; i++) {
        if (round.events[i].f > f) { cur = round.events[i].f; draw(); return; }
      }
    });
    document.getElementById('replay-prevkill').addEventListener('click', function () {
      var f = Math.floor(cur), target = 0;
      for (var i = 0; i < round.events.length; i++) {
        if (round.events[i].f < f) target = round.events[i].f; else break;
      }
      cur = target; draw();
    });
    speedBtn.addEventListener('click', function () {
      speed = speed === 1 ? 2 : speed === 2 ? 4 : 1;
      speedBtn.innerHTML = speed + '&times;';
    });
    var nadeSeg = document.getElementById('replay-nadeseg');
    if (nadeSeg) nadeSeg.addEventListener('click', function (e) {
      if (e.target.tagName !== 'BUTTON') return;
      nadeSeg.querySelectorAll('button').forEach(function (b) {
        b.classList.toggle('active', b === e.target);
      });
      showNades = e.target.dataset.val === 'on';
      draw();
    });
    redrawReplay = draw;                          // let the focus controller redraw
    resetFeed = function () { feedSig = ''; };    // ...and force a killfeed rebuild
    loadRound(0);
  }

  // Scroll-spy: highlight the dot of the round currently under the sticky bar.
  if ('IntersectionObserver' in window) {
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var n = en.target.id.split('-')[1];
        dots.forEach(function (d) { d.classList.toggle('current', d.dataset.round === n); });
      });
    }, { rootMargin: '-' + (sticky.offsetHeight + 20) + 'px 0px -70% 0px', threshold: 0 });
    cards.forEach(function (c) { spy.observe(c); });
  }

  applyFocus();     // render Layer 4 + rule badges and normalise everything to the default focus
})();
</script>
"""

PAGE_FOOT = "</div>\n" + PAGE_SCRIPT + "</body>\n</html>\n"


def name_class(rel_str, is_me) -> str:
    return "me" if is_me else rel_str   # 'ally' / 'enemy' / ''


def render_sticky(summary, rounds, meta) -> str:
    my, opp = summary["my_final"], summary["opp_final"]
    res = "win" if summary["won_match"] else "loss"
    res_lbl = "Victory" if summary["won_match"] else "Defeat"
    k, d = summary["ref_kills"], summary["ref_deaths"]
    ref = esc(meta["ref_name"])

    dots = []
    for rd in rounds:
        mk, md = rd["my_kills"], rd["my_deaths"]
        color = "green" if mk > md else "red" if md > mk else "gray"
        dots.append(f'<a class="dot {color}" href="#round-{rd["number"]}" '
                    f'data-round="{rd["number"]}" '
                    f'title="Round {rd["number"]}: {mk}K {md}D">{rd["number"]}</a>')

    filters = [
        '<button class="filt active" data-filter="all">All rounds</button>',
        '<button class="filt" data-filter="deaths">My deaths only</button>',
        '<button class="filt" data-filter="multi">My multi-kills (2+)</button>',
        '<button class="filt" data-filter="clutch">Clutch rounds</button>',
    ]
    if meta["rival_found"]:
        title = (f'Your nemesis &mdash; killed you {meta["rival_kills_on_me"]}x'
                 if meta["rival_auto"] else "Your chosen rival")
        filters.append(f'<button class="filt" data-filter="rival" title="{title}">'
                       f'vs {esc(meta["rival_name"])}</button>')

    return (
        '<div class="sticky">'
        '<div class="bar summary-bar">'
        '<span class="brand">demoReview</span>'
        f'<span class="sscore"><span class="you">{my}</span>'
        f'<span class="dash">&ndash;</span><span class="opp">{opp}</span></span>'
        f'<span class="pill {res}">{res_lbl}</span>'
        f'<span class="muted">{summary["total_kills"]} kills</span>'
        f'<span class="mekd"><b>{ref}</b> &nbsp;'
        f'<span class="you">{k}K</span> / <span class="opp">{d}D</span> &nbsp;'
        f'&middot; {kd_ratio(k, d)} KD</span>'
        '</div>'
        f'<div class="bar jump-bar">{"".join(dots)}</div>'
        f'<div class="bar filter-bar">{"".join(filters)}</div>'
        '</div>'
    )


def _kill_name(name, rel, is_me, sid) -> str:
    """A clickable player name inside a kill line (colour baked for default focus)."""
    sid_attr = f' data-sid="{esc(sid)}"' if sid else ""
    pn = " pn" if sid else ""
    return (f'<span class="name{pn} {name_class(rel, is_me)}"{sid_attr}>{esc(name)}</span>')


def render_kill(kill, is_opening=False, rnd=None, idx=None) -> str:
    classes = ["kill"]
    if kill["attacker_is_me"]:
        classes.append("mine-kill")
    elif kill["victim_is_me"]:
        classes.append("mine-death")

    attacker = _kill_name(kill["attacker"], kill["attacker_rel"],
                          kill["attacker_is_me"], kill.get("attacker_sid"))
    victim = _kill_name(kill["victim"], kill["victim_rel"],
                        kill["victim_is_me"], kill.get("victim_sid"))
    opening = ' <span class="open">OPENING</span>' if is_opening else ""
    hs = ' <span class="hs">HS</span>' if kill["headshot"] else ""
    tag = ""
    if kill["attacker_is_me"]:
        tag = ' <span class="me-tag kill">FOCUS KILL</span>'
    elif kill["victim_is_me"]:
        tag = ' <span class="me-tag death">FOCUS DEATH</span>'

    data = (f' data-ka="{esc(kill.get("attacker_sid") or "")}" '
            f'data-kv="{esc(kill.get("victim_sid") or "")}" '
            f'data-round="{rnd}" data-idx="{idx}"')
    return (f'<div class="{" ".join(classes)}"{data}>{attacker}'
            f'<span class="arrow">&#9656;</span>{victim}'
            f'<span class="wep">{esc(kill["weapon"])}</span>{opening}{hs}{tag}'
            f'<span class="rbadge-slot"></span></div>')


MULTI_LABEL = {2: "2K", 3: "3K", 4: "4K", 5: "ACE"}


def _notable_clutch(c, ref_sid) -> bool:
    """A clutch worth a badge/filter: won by anyone, or the ref's own 1v2+.

    Every round has *some* last-alive player (a technical 1vX they usually lose),
    so we only surface won clutches plus the highlighted player's real attempts.
    """
    return c["won"] or (c["sid"] == ref_sid and c["x"] >= 2)


def render_insight_badges(insights, ref_sid) -> str:
    """Clutch (won / your own) and big-multi (3K+) badges for a round."""
    badges = []
    for c in insights.get("clutches", []):
        if not _notable_clutch(c, ref_sid):
            continue
        cls = "clutch-won" if c["won"] else "clutch-lost"
        mine = " mine" if c["sid"] == ref_sid else ""
        verb = "WON" if c["won"] else "LOST"
        badges.append(f'<span class="ibadge {cls}{mine}">'
                      f'{esc(c["name"])} 1v{c["x"]} {verb}</span>')
    bm = insights.get("best_multi")
    if bm and bm["count"] >= 3:
        mine = " mine" if bm["sid"] == ref_sid else ""
        badges.append(f'<span class="ibadge multi{mine}">'
                      f'{esc(bm["name"])} {MULTI_LABEL.get(bm["count"], str(bm["count"]) + "K")}</span>')
    if not badges:
        return ""
    return '<div class="ibadges">' + "".join(badges) + '</div>'


def render_round(rd, ref_sid=None) -> str:
    mk, md = rd["my_kills"], rd["my_deaths"]
    insights = rd.get("insights") or {}
    has_clutch = 1 if any(_notable_clutch(c, ref_sid)
                          for c in insights.get("clutches", [])) else 0
    parts = [f'<div class="card round" id="round-{rd["number"]}" '
             f'data-mk="{mk}" data-md="{md}" data-rival="{1 if rd["vs_rival"] else 0}" '
             f'data-clutch="{has_clutch}">']
    parts.append('<div class="round-head">')
    parts.append(f'<span class="round-no">Round {rd["number"]}</span>')
    result = "win" if rd["won"] else "loss"
    label = "Your team won" if rd["won"] else "Opponent won"
    parts.append(f'<span class="badge {result}">{label}</span>')
    parts.append(f'<span class="played">you played {esc(rd["my_side_label"])}</span>')
    parts.append(f'<span class="reason">{esc(rd["reason"])}</span>')
    parts.append(f'<span class="rscore"><span class="you">You {rd["my_score"]}</span> '
                 f'&ndash; <span class="opp">{rd["opp_score"]} Opp</span></span>')
    parts.append('</div>')

    badges = render_insight_badges(insights, ref_sid)
    if badges:
        parts.append(badges)

    meta = []
    alive_txt = " &nbsp; ".join(
        f'{label}: <b>{me}v{op}</b>' for (label, me, op) in rd["alive"]
    )
    meta.append(f'alive (you v opp) &mdash; {alive_txt}')
    meta.append(f'you this round: <b>{mk}K</b> / <b>{md}D</b>')
    if rd["kills"]:
        meta.append(render_opening(rd["kills"][0]))
    parts.append('<div class="meta">' + "".join(f'<span>{m}</span>' for m in meta) + '</div>')

    if rd["economy"]:
        parts.append(render_economy(rd["economy"]))

    if rd["kills"]:
        parts.extend(render_kill(k, is_opening=(i == 0), rnd=rd["number"], idx=i)
                     for i, k in enumerate(rd["kills"]))
    else:
        parts.append('<div class="no-kills">No kills this round.</div>')

    parts.append('</div>')
    return "".join(parts)


def render_opening(first_kill) -> str:
    """Meta line describing who drew first blood (from your team's view)."""
    rel_str = first_kill["attacker_rel"]
    if rel_str == "ally":
        who = '<span class="you">Your team</span> drew first blood'
    elif rel_str == "enemy":
        who = '<span class="opp">Opponent</span> drew first blood'
    else:
        who = 'first blood'
    if first_kill["attacker_is_me"]:
        who += ' &mdash; <b class="name me">your entry frag!</b>'
    elif first_kill["victim_is_me"]:
        who += ' &mdash; <b class="opening-died">you died first</b>'
    return f'opening &mdash; {who}'


def render_economy(economy) -> str:
    """Collapsible per-player economy breakdown, You vs Opponent."""
    you, opp = economy["you"], economy["opp"]
    summary = (f'<span class="you">You</span> <b>{you["buytype"]}</b> (${you["total"]:,}) '
               f'&nbsp;vs&nbsp; <span class="opp">Opp</span> '
               f'<b>{opp["buytype"]}</b> (${opp["total"]:,})')

    def column(side, label, cls):
        rows = [f'<div class="econ-head {cls}">{label} &middot; '
                f'{side["buytype"]} buy &middot; ${side["total"]:,}</div>']
        for p in side["players"]:
            me = " me" if p["is_me"] else ""
            rows.append(
                f'<div class="econ-player{me}">'
                f'<span class="pname">{esc(p["name"])}</span>'
                f'<span class="ptype">{buy_type(p["equip"])}</span>'
                f'<span class="pmoney">${p["equip"]:,} equip &middot; ${p["cash"]:,} bank</span>'
                f'</div>')
        return '<div class="econ-col">' + "".join(rows) + '</div>'

    return (f'<details class="econ"><summary>{summary}</summary>'
            f'<div class="econ-grid">'
            f'{column(you, "Your team", "you")}{column(opp, "Opponent", "opp")}'
            f'</div></details>')


def render_your_match(rs, ref_name) -> str:
    """A strip of headline pro stats for the highlighted player."""
    if not rs:
        return ""
    multi = sum(rs["multi"].values())
    chips = [
        ("Rating", f"{rs['rating']:.2f}", True),
        ("ADR", f"{rs['adr']:.0f}", True),
        ("KAST", f"{rs['kast']:.0f}%", True),
        ("K / D / A", f"{rs['kills']} / {rs['deaths']} / {rs['assists']}", False),
        ("HS%", f"{rs['hs_pct']:.0f}%", False),
        ("Opening", f"{rs['opening_k']}-{rs['opening_d']} ({rs['open_pct']:.0f}%)", False),
        ("Multi-kills", f"{multi}", False),
        ("Trade kills", f"{rs['trade_kills']}", False),
        ("Clutches", f"{rs['clutch_won']}/{rs['clutch']}", False),
        ("Flash assists", f"{rs['flash_assists']}", False),
    ]
    inner = "".join(
        f'<div class="chip{" hi" if hi else ""}">'
        f'<span class="clabel">{esc(label)}</span>'
        f'<span class="cval">{esc(val)}</span></div>'
        for label, val, hi in chips)
    return (f'<div class="your-match" id="ym-host"><div class="ym-title">'
            f'<span id="ym-name">{esc(ref_name)}</span> &mdash; focus player</div>'
            f'<div class="chips" id="ym-chips">{inner}</div></div>')


SB_COLUMNS = [
    ("name", "Player", "left"), ("team", "Team", "left"),
    ("kills", "K", "num"), ("deaths", "D", "num"), ("assists", "A", "num"),
    ("adr", "ADR", "num"), ("kast", "KAST", "num"), ("hs", "HS%", "num"),
    ("rating", "Rating", "num"),
]


def render_scoreboard(players, ref_sid) -> str:
    """Sortable pro scoreboard table (default sort: rating, descending)."""
    heads = []
    for key, label, cls in SB_COLUMNS:
        th_cls = "num" if cls == "num" else ""
        extra = ""
        if key == "rating":
            th_cls = (th_cls + " sorted").strip()
            extra = ' data-dir="desc"'
        heads.append(f'<th data-sort="{key}" class="{th_cls}"{extra}>{esc(label)}</th>')

    rows = []
    for p in players:
        me = ' class="me"' if p["sid"] == ref_sid else ""
        team_code = "A" if p["on_your_team"] else "B"
        team = ('<span class="tag you">You</span>' if p["on_your_team"]
                else '<span class="tag opp">Opp</span>')
        rows.append(
            f'<tr{me} data-sid="{esc(p["sid"])}" data-team="{team_code}" '
            f'data-name="{esc(p["name"])}" data-kills="{p["kills"]}" '
            f'data-deaths="{p["deaths"]}" data-assists="{p["assists"]}" '
            f'data-adr="{p["adr"]:.1f}" data-kast="{p["kast"]:.1f}" '
            f'data-hs="{p["hs_pct"]:.1f}" data-rating="{p["rating"]:.3f}">'
            f'<td><span class="pn" data-sid="{esc(p["sid"])}">{esc(p["name"])}</span></td>'
            f'<td class="teamtag">{team}</td>'
            f'<td class="num">{p["kills"]}</td><td class="num">{p["deaths"]}</td>'
            f'<td class="num">{p["assists"]}</td><td class="num">{p["adr"]:.0f}</td>'
            f'<td class="num">{p["kast"]:.0f}%</td><td class="num">{p["hs_pct"]:.0f}%</td>'
            f'<td class="num rating">{p["rating"]:.2f}</td></tr>')

    return (f'<table id="scoreboard" class="sb"><thead><tr>{"".join(heads)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _map_dot(px, py, kind, is_me, title, sid=None) -> str:
    me = " me" if is_me else ""
    sid_attr = f' data-sid="{esc(sid)}"' if sid else ""
    return (f'<circle class="d {kind}{me}" data-type="{kind}" data-me="{1 if is_me else 0}"{sid_attr} '
            f'cx="{px:.1f}" cy="{py:.1f}" r="6"><title>{esc(title)}</title></circle>')


NADE_LABEL = {"smoke": "smoke", "molotov": "molotov", "he": "HE",
              "flash": "flash", "decoy": "decoy"}


def _nade_map_svg(nade) -> str:
    """A detonation dot plus a faint line back to where it was thrown from."""
    me = 1 if nade["me"] else 0
    t = nade["t"]
    title = (f'R{nade["round"]} · {NADE_LABEL.get(t, t)} detonation'
             + (' · you' if nade["me"] else ''))
    line = (f'<line class="d nade {t}" data-type="nade" data-me="{me}" '
            f'x1="{nade["ox"]:.1f}" y1="{nade["oy"]:.1f}" '
            f'x2="{nade["dx"]:.1f}" y2="{nade["dy"]:.1f}"></line>')
    dot = (f'<circle class="d nade {t}" data-type="nade" data-me="{me}" '
           f'cx="{nade["dx"]:.1f}" cy="{nade["dy"]:.1f}" r="5">'
           f'<title>{esc(title)}</title></circle>')
    return line + dot


def render_map(map_kills, map_nades, meta, info) -> str:
    """Radar panel with kill/death dots (radar image via the shared .radar-bg)."""
    size = info["size"]

    dots = []
    for mk in map_kills:
        if mk["kx"] is not None and mk["ky"] is not None:
            px, py = maps.world_to_radar(mk["kx"], mk["ky"], info)
            dots.append(_map_dot(px, py, "kill", mk["killer_is_me"],
                                 f'R{mk["round"]} · {mk["killer"]} killed {mk["victim"]} · {mk["weapon"]}',
                                 sid=mk.get("killer_sid")))
        if mk["vx"] is not None and mk["vy"] is not None:
            px, py = maps.world_to_radar(mk["vx"], mk["vy"], info)
            dots.append(_map_dot(px, py, "death", mk["victim_is_me"],
                                 f'R{mk["round"]} · {mk["victim"]} killed by {mk["killer"]} · {mk["weapon"]}',
                                 sid=mk.get("victim_sid")))
    nade_svg = "".join(_nade_map_svg(n) for n in (map_nades or []))

    nade_seg = (
        '<div class="seg" data-group="nades">'
        '<button data-val="off" class="active">Nades off</button>'
        '<button data-val="on">Nades on</button></div>'
    ) if map_nades else ""
    nade_legend = (
        '<div class="nade-legend">Grenade detonations: '
        '<span><i class="nl-smoke"></i>smoke</span>'
        '<span><i class="nl-molotov"></i>molotov</span>'
        '<span><i class="nl-he"></i>HE</span>'
        '<span><i class="nl-flash"></i>flash</span>'
        '<span><i class="nl-decoy"></i>decoy</span>'
        '<span class="muted">— line points back to the throw origin</span></div>'
    ) if map_nades else ""

    controls = (
        '<div class="map-controls">'
        '<div class="seg" data-group="scope">'
        '<button data-val="you" class="active">You</button>'
        '<button data-val="all">Everyone</button></div>'
        '<div class="seg" data-group="type">'
        '<button data-val="both" class="active">Kills + deaths</button>'
        '<button data-val="kills">Kills</button>'
        '<button data-val="deaths">Deaths</button></div>'
        f'{nade_seg}'
        '<span class="map-legend"><i class="lk"></i> kills &nbsp; <i class="ld"></i> deaths</span>'
        '</div>'
    )
    return (
        '<div class="card mapcard">'
        f'<div class="rounds-title" style="margin:0 0 12px">Map &mdash; {esc(meta["map_name"])}'
        ' <span class="muted" style="text-transform:none;letter-spacing:0">'
        '(where you get kills vs where you die)</span></div>'
        f'{controls}'
        '<div class="mapwrap" id="mapwrap">'
        '<div class="radar-bg"></div>'
        f'<svg viewBox="0 0 {size} {size}" preserveAspectRatio="xMidYMid meet">{"".join(dots)}{nade_svg}</svg>'
        '</div>'
        f'{nade_legend}</div>'
    )


def render_replay(replay, meta, info) -> str:
    """2D canvas round-replay panel with play/scrub/kill-jump controls."""
    if not replay or not replay.get("rounds"):
        return ""
    size = replay["size"]
    options = "".join(f'<option value="{i}">Round {rd["n"]}</option>'
                      for i, rd in enumerate(replay["rounds"]))
    data_json = embed_json(replay)
    return (
        '<div class="card replaycard">'
        '<div class="rounds-title" style="margin:0 0 12px">Round replay '
        '<span class="muted" style="text-transform:none;letter-spacing:0">'
        '(2D playback — you are the ringed green dot)</span></div>'
        '<div class="replay-controls">'
        '<button class="rbtn" id="replay-prevround" title="Previous round">&#9664;</button>'
        f'<select id="replay-round" class="rsel">{options}</select>'
        '<button class="rbtn" id="replay-nextround" title="Next round">&#9654;</button>'
        '<button class="rbtn play" id="replay-play" title="Play/pause">&#9654;</button>'
        '<button class="rbtn" id="replay-prevkill" title="Previous kill">&#9198;</button>'
        '<button class="rbtn" id="replay-nextkill" title="Next kill">&#9197;</button>'
        '<button class="rbtn" id="replay-speed" title="Speed">2&times;</button>'
        '<div class="seg" id="replay-nadeseg" data-group="nades">'
        '<button data-val="on" class="active">Nades on</button>'
        '<button data-val="off">Nades off</button></div>'
        '<span class="rtime" id="replay-time">0.0s</span>'
        '</div>'
        '<input type="range" id="replay-scrub" class="rscrub" min="0" max="0" value="0">'
        '<div class="replaywrap">'
        '<div class="radar-bg"></div>'
        f'<canvas id="replay-canvas" width="{size}" height="{size}"></canvas>'
        '<div class="killfeed" id="replay-feed"></div>'
        '</div>'
        '<div class="nade-legend">Nades: '
        '<span><i class="nl-smoke"></i>smoke</span>'
        '<span><i class="nl-molotov"></i>molotov</span>'
        '<span><i class="nl-he"></i>HE</span>'
        '<span><i class="nl-flash"></i>flash</span>'
        '<span><i class="nl-decoy"></i>decoy</span>'
        '<span class="muted">— trail shows the throw arc/direction</span></div>'
        f'<script type="application/json" id="replay-data">{data_json}</script>'
        '</div>'
    )


def render_html(summary, rounds, demo_name, meta, coaching_md=None, site_nav=None) -> str:
    my, opp = summary["my_final"], summary["opp_final"]
    ref = meta["ref_name"]
    ref_sid = meta.get("ref_sid")
    info = maps.map_info(meta.get("map_name"))
    radar_uri = maps.radar_data_uri(info) if info else None

    parts = [PAGE_HEAD]
    if site_nav:
        # Optional slim site nav (the web app injects one; the CLI passes nothing).
        parts.append(site_nav)
    if radar_uri:
        # Radar image embedded once; shared by the heatmap and replay panels.
        parts.append(f'<style>.radar-bg{{background-image:url("{radar_uri}");'
                     'background-size:cover;background-position:center}}</style>')
    parts.append(render_sticky(summary, rounds, meta))

    sub = (f'{esc(demo_name)} &nbsp;&middot;&nbsp; focus player: '
           f'<span class="name me" id="sub-focus">{esc(ref)}</span>'
           ' <span class="muted">&mdash; click any player name to spotlight them</span>')
    if not meta["found"]:
        sub += ' <span class="opp">(not found &mdash; showing CT-start team)</span>'
    if meta["rival_found"] and meta["rival_auto"]:
        sub += (f' &nbsp;&middot;&nbsp; nemesis: <span class="opp">{esc(meta["rival_name"])}</span>'
                f' (killed you {meta["rival_kills_on_me"]}&times;)')
    parts.append(f'<div class="sub">{sub}</div>')

    # Summary card.
    result = "win" if summary["won_match"] else "loss"
    result_lbl = "Victory" if summary["won_match"] else "Defeat"
    parts.append('<div class="card">')
    parts.append('<div class="score-big">'
                 f'<span class="you">Your team {my}</span>'
                 f'<span style="color:var(--muted)">&ndash;</span>'
                 f'<span class="opp">{opp} Opponent</span>'
                 f'<span class="result {result}">{result_lbl}</span></div>')
    parts.append(f'<div class="stat-row"><span>{summary["n_rounds"]} rounds</span>'
                 f'<span>{summary["total_kills"]} kills</span></div>')
    parts.append(render_your_match(summary.get("ref_stats"), ref))
    parts.append(render_scoreboard(summary["players"], ref_sid))
    parts.append('<div class="note">Score is the engine\'s native per-team total, '
                 'correct across the halftime side swap. Rating approximates HLTV 2.0. '
                 'Click a column header to sort.</div>')
    parts.append('</div>')

    # Layer 6: pre-generated AI coaching report (baked in at build time; the
    # button just toggles it -- no live/browser-side API call, no key exposure).
    parts.append(render_coaching(coaching_md))

    if info:
        parts.append(render_map(summary.get("map_kills", []),
                                summary.get("map_nades", []), meta, info))
        parts.append(render_replay(summary.get("replay"), meta, info))

    parts.append('<div class="rounds-title">Rounds</div>')
    parts.extend(render_round(rd, ref_sid) for rd in rounds)
    parts.append('<div class="empty">No rounds match this filter.</div>')

    # Layer 4: per-player breakdown charts (filled client-side, follows focus).
    parts.append('<div class="rounds-title" id="breakdown-title">Player breakdown '
                 '<span class="muted" style="text-transform:none;letter-spacing:0">'
                 '(follows the focus player)</span></div>')
    parts.append('<div class="card" id="layer4"></div>')

    # Embedded per-player analysis blob for the client-side focus switch.
    focus_blob = dict(summary.get("analysis") or {})
    focus_blob["focus"] = ref_sid
    parts.append('<script type="application/json" id="focus-data">'
                 f'{embed_json(focus_blob)}</script>')

    parts.append(PAGE_FOOT)
    return "".join(parts)
