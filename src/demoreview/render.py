"""HTML/CSS/JS rendering of the report (single self-contained document)."""

import html
import json

from . import maps
from .parsing import buy_type


def esc(text) -> str:
    return html.escape(str(text))


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
<style>
  :root {
    --bg: #0e1116; --card: #171b22; --card2: #1e232c; --line: #262c37;
    --text: #e6e8eb; --muted: #9aa2ad;
    --you: #6ea8fe; --opp: #ff8f6b;          /* your team / opponent */
    --me: #35d07f; --me-death: #ff6b81;       /* highlighted player */
    --win: #35d07f; --loss: #ff6b81;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5; padding: 0 16px 40px;
  }
  .wrap { max-width: 880px; margin: 0 auto; }

  /* Sticky header (score + jump bar + filters) */
  .sticky {
    position: sticky; top: 0; z-index: 50;
    background: rgba(14,17,22,.86); backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border: 1px solid var(--line); border-top: none;
    border-radius: 0 0 14px 14px; padding: 12px 18px; margin-bottom: 22px;
  }
  .bar { display: flex; align-items: center; gap: 8px 14px; flex-wrap: wrap; }
  .bar + .bar { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--line); }
  .brand { font-weight: 700; letter-spacing: -0.02em; }
  .sscore { font-size: 1.05rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .sscore .dash { color: var(--muted); margin: 0 5px; }
  .you { color: var(--you); } .opp { color: var(--opp); }
  .pill { font-size: .68rem; font-weight: 700; padding: 2px 9px; border-radius: 999px;
          text-transform: uppercase; letter-spacing: .04em; }
  .pill.win { background: rgba(53,208,127,.16); color: var(--win); }
  .pill.loss { background: rgba(255,107,129,.16); color: var(--loss); }
  .muted { color: var(--muted); }
  .mekd { margin-left: auto; color: var(--muted); font-size: .85rem; }
  .mekd b { color: var(--text); }

  .jump-bar { gap: 6px; }
  .dot { display: inline-flex; align-items: center; justify-content: center;
         width: 26px; height: 26px; border-radius: 50%; font-size: .72rem;
         font-weight: 700; text-decoration: none; color: var(--text);
         border: 1px solid transparent; cursor: pointer; transition: transform .08s; }
  .dot:hover { transform: translateY(-1px); }
  .dot.green { background: rgba(53,208,127,.18); color: #7ee6ab; }
  .dot.red   { background: rgba(255,107,129,.16); color: #ff9aa8; }
  .dot.gray  { background: var(--card2); color: var(--muted); }
  .dot.current { outline: 2px solid var(--you); outline-offset: 1px; }

  .filter-bar { gap: 8px; }
  .filt { font-family: inherit; font-size: .8rem; color: var(--muted);
          background: var(--card2); border: 1px solid var(--line);
          padding: 5px 12px; border-radius: 8px; cursor: pointer; }
  .filt:hover { color: var(--text); }
  .filt.active { background: var(--you); color: #0b1220; border-color: var(--you); font-weight: 600; }

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
  .result.win { background: rgba(53,208,127,.16); color: var(--win); }
  .result.loss { background: rgba(255,107,129,.16); color: var(--loss); }
  .stat-row { display: flex; gap: 28px; flex-wrap: wrap; margin-top: 8px; color: var(--muted); font-size: .9rem; }
  table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: .92rem; }
  th { text-align: left; color: var(--muted); font-weight: 600; font-size: .78rem;
       text-transform: uppercase; letter-spacing: .04em; padding: 6px 10px; border-bottom: 1px solid var(--line); }
  td { padding: 7px 10px; border-bottom: 1px solid var(--line); }
  tr:last-child td { border-bottom: none; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .tag { font-size: .68rem; font-weight: 700; padding: 1px 7px; border-radius: 999px; }
  .tag.you { background: rgba(110,168,254,.16); color: var(--you); }
  .tag.opp { background: rgba(255,143,107,.16); color: var(--opp); }
  tr.me td { background: rgba(53,208,127,.12); }
  tr.me td:first-child { border-left: 3px solid var(--me); font-weight: 700; }
  .rounds-title { color: var(--muted); font-size: .8rem; text-transform: uppercase;
                  letter-spacing: .05em; margin: 30px 0 12px; }
  .round { scroll-margin-top: 150px; }
  .round-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
  .round-no { font-weight: 700; font-size: 1.1rem; }
  .badge { font-size: .72rem; font-weight: 700; padding: 3px 9px; border-radius: 999px;
           text-transform: uppercase; letter-spacing: .03em; }
  .badge.win { background: rgba(53,208,127,.18); color: var(--win); }
  .badge.loss { background: rgba(255,107,129,.18); color: var(--loss); }
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
  .kill.mine-kill { background: rgba(53,208,127,.12); border-left-color: var(--me); }
  .kill.mine-death { background: rgba(255,107,129,.10); border-left-color: var(--me-death); }
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
  .sb th.sorted[data-dir="desc"]::after { content: " \25be"; }
  .sb th.sorted[data-dir="asc"]::after { content: " \25b4"; }
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
  .ibadge.clutch-won { background: rgba(53,208,127,.18); color: var(--win); }
  .ibadge.clutch-lost { background: rgba(255,107,129,.14); color: var(--loss); }
  .ibadge.trade { background: rgba(110,168,254,.16); color: var(--you); }
  .ibadge.multi { background: rgba(199,146,234,.18); color: #c792ea; }
  .ibadge.mine { outline: 1px solid var(--me); outline-offset: 1px; }

  /* Map heatmap */
  .map-controls { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; margin-bottom: 14px; }
  .seg { display: inline-flex; background: var(--card2); border: 1px solid var(--line);
         border-radius: 9px; overflow: hidden; }
  .seg button { font: inherit; font-size: .78rem; color: var(--muted); background: transparent;
                border: 0; padding: 5px 11px; cursor: pointer; }
  .seg button + button { border-left: 1px solid var(--line); }
  .seg button.active { background: var(--you); color: #0b1220; font-weight: 600; }
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

  /* Round replay */
  .replay-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
  .rbtn { font: inherit; font-size: .82rem; color: var(--text); background: var(--card2);
          border: 1px solid var(--line); border-radius: 8px; padding: 5px 10px; cursor: pointer; min-width: 34px; }
  .rbtn:hover { border-color: var(--you); }
  .rbtn.play { background: var(--you); color: #0b1220; font-weight: 700; }
  .rsel { font: inherit; font-size: .82rem; color: var(--text); background: var(--card2);
          border: 1px solid var(--line); border-radius: 8px; padding: 5px 8px; }
  .rtime { font-variant-numeric: tabular-nums; color: var(--muted); font-size: .82rem; min-width: 44px; }
  .revt { color: var(--muted); font-size: .8rem; margin-left: auto; }
  .rscrub { width: 100%; max-width: 560px; margin: 0 auto 10px; display: block; accent-color: var(--you); }
</style>
</head>
<body>
<div class="wrap">
"""

PAGE_SCRIPT = """<script>
(function () {
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
    var dots = Array.prototype.slice.call(mapwrap.querySelectorAll('circle.d'));
    var mapScope = 'you', mapType = 'both';
    function updateMap() {
      dots.forEach(function (c) {
        var scopeOk = (mapScope === 'all') || c.dataset.me === '1';
        var dt = c.dataset.type;
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
        if (seg.dataset.group === 'scope') mapScope = e.target.dataset.val;
        else mapType = e.target.dataset.val;
        updateMap();
      });
    });
    updateMap();
  }

  // 2D round replay.
  var replayEl = document.getElementById('replay-data');
  if (replayEl) {
    var RP = JSON.parse(replayEl.textContent);
    var cv = document.getElementById('replay-canvas');
    var ctx = cv.getContext('2d');
    var COLORS = { you: '#35d07f', ally: '#6ea8fe', enemy: '#ff8f6b' };
    var sel = document.getElementById('replay-round');
    var scrub = document.getElementById('replay-scrub');
    var playBtn = document.getElementById('replay-play');
    var speedBtn = document.getElementById('replay-speed');
    var timeLbl = document.getElementById('replay-time');
    var evtLbl = document.getElementById('replay-evt');
    var round = RP.rounds[0], cur = 0, playing = false, speed = 2, last = 0;

    function fps() { return 64 / round.step; }          // sample frames per second

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
    function draw() {
      ctx.clearRect(0, 0, cv.width, cv.height);
      round.players.forEach(function (p) {
        var pos = posAt(p, cur);
        if (!pos) return;
        var col = COLORS[p.r] || '#aaa';
        var yr = pos.yaw * Math.PI / 180;               // yaw 0=east; radar y is flipped
        ctx.strokeStyle = col; ctx.lineWidth = 4;
        ctx.beginPath(); ctx.moveTo(pos.x, pos.y);
        ctx.lineTo(pos.x + Math.cos(yr) * 26, pos.y - Math.sin(yr) * 26); ctx.stroke();
        ctx.beginPath(); ctx.arc(pos.x, pos.y, p.r === 'you' ? 15 : 12, 0, 6.2832);
        ctx.fillStyle = col; ctx.fill();
        if (p.r === 'you') { ctx.lineWidth = 5; ctx.strokeStyle = '#fff'; ctx.stroke(); }
      });
      var f = Math.floor(cur);
      scrub.value = f;
      timeLbl.textContent = (f / fps()).toFixed(1) + 's';
      var ev = '';
      for (var i = 0; i < round.events.length; i++) {
        if (round.events[i][0] <= f) ev = round.events[i][2]; else break;
      }
      evtLbl.textContent = ev;
    }
    function loadRound(i) {
      round = RP.rounds[i]; cur = 0; playing = false; playBtn.innerHTML = '&#9654;';
      scrub.max = round.nf - 1; scrub.value = 0; sel.value = i; draw();
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
        if (round.events[i][0] > f) { cur = round.events[i][0]; draw(); return; }
      }
    });
    document.getElementById('replay-prevkill').addEventListener('click', function () {
      var f = Math.floor(cur), target = 0;
      for (var i = 0; i < round.events.length; i++) {
        if (round.events[i][0] < f) target = round.events[i][0]; else break;
      }
      cur = target; draw();
    });
    speedBtn.addEventListener('click', function () {
      speed = speed === 1 ? 2 : speed === 2 ? 4 : 1;
      speedBtn.innerHTML = speed + '&times;';
    });
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


def render_kill(kill, is_opening=False) -> str:
    classes = ["kill"]
    if kill["attacker_is_me"]:
        classes.append("mine-kill")
    elif kill["victim_is_me"]:
        classes.append("mine-death")

    attacker = (f'<span class="name {name_class(kill["attacker_rel"], kill["attacker_is_me"])}">'
                f'{esc(kill["attacker"])}</span>')
    victim = (f'<span class="name {name_class(kill["victim_rel"], kill["victim_is_me"])}">'
              f'{esc(kill["victim"])}</span>')
    opening = ' <span class="open">OPENING</span>' if is_opening else ""
    hs = ' <span class="hs">HS</span>' if kill["headshot"] else ""
    tag = ""
    if kill["attacker_is_me"]:
        tag = ' <span class="me-tag kill">YOUR KILL</span>'
    elif kill["victim_is_me"]:
        tag = ' <span class="me-tag death">YOUR DEATH</span>'

    return (f'<div class="{" ".join(classes)}">{attacker}'
            f'<span class="arrow">&#9656;</span>{victim}'
            f'<span class="wep">{esc(kill["weapon"])}</span>{opening}{hs}{tag}</div>')


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
        parts.extend(render_kill(k, is_opening=(i == 0))
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
    return (f'<div class="your-match"><div class="ym-title">'
            f'{esc(ref_name)} &mdash; your match</div>'
            f'<div class="chips">{inner}</div></div>')


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
        team = ('<span class="tag you">You</span>' if p["on_your_team"]
                else '<span class="tag opp">Opp</span>')
        rows.append(
            f'<tr{me} data-name="{esc(p["name"])}" data-kills="{p["kills"]}" '
            f'data-deaths="{p["deaths"]}" data-assists="{p["assists"]}" '
            f'data-adr="{p["adr"]:.1f}" data-kast="{p["kast"]:.1f}" '
            f'data-hs="{p["hs_pct"]:.1f}" data-rating="{p["rating"]:.3f}">'
            f'<td>{esc(p["name"])}</td><td>{team}</td>'
            f'<td class="num">{p["kills"]}</td><td class="num">{p["deaths"]}</td>'
            f'<td class="num">{p["assists"]}</td><td class="num">{p["adr"]:.0f}</td>'
            f'<td class="num">{p["kast"]:.0f}%</td><td class="num">{p["hs_pct"]:.0f}%</td>'
            f'<td class="num rating">{p["rating"]:.2f}</td></tr>')

    return (f'<table id="scoreboard" class="sb"><thead><tr>{"".join(heads)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _map_dot(px, py, kind, is_me, title) -> str:
    me = " me" if is_me else ""
    return (f'<circle class="d {kind}{me}" data-type="{kind}" data-me="{1 if is_me else 0}" '
            f'cx="{px:.1f}" cy="{py:.1f}" r="6"><title>{esc(title)}</title></circle>')


def render_map(map_kills, meta, info) -> str:
    """Radar panel with kill/death dots (radar image via the shared .radar-bg)."""
    size = info["size"]

    dots = []
    for mk in map_kills:
        if mk["kx"] is not None and mk["ky"] is not None:
            px, py = maps.world_to_radar(mk["kx"], mk["ky"], info)
            dots.append(_map_dot(px, py, "kill", mk["killer_is_me"],
                                 f'R{mk["round"]} · {mk["killer"]} killed {mk["victim"]} · {mk["weapon"]}'))
        if mk["vx"] is not None and mk["vy"] is not None:
            px, py = maps.world_to_radar(mk["vx"], mk["vy"], info)
            dots.append(_map_dot(px, py, "death", mk["victim_is_me"],
                                 f'R{mk["round"]} · {mk["victim"]} killed by {mk["killer"]} · {mk["weapon"]}'))

    controls = (
        '<div class="map-controls">'
        '<div class="seg" data-group="scope">'
        '<button data-val="you" class="active">You</button>'
        '<button data-val="all">Everyone</button></div>'
        '<div class="seg" data-group="type">'
        '<button data-val="both" class="active">Kills + deaths</button>'
        '<button data-val="kills">Kills</button>'
        '<button data-val="deaths">Deaths</button></div>'
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
        f'<svg viewBox="0 0 {size} {size}" preserveAspectRatio="xMidYMid meet">{"".join(dots)}</svg>'
        '</div></div>'
    )


def render_replay(replay, meta, info) -> str:
    """2D canvas round-replay panel with play/scrub/kill-jump controls."""
    if not replay or not replay.get("rounds"):
        return ""
    size = replay["size"]
    options = "".join(f'<option value="{i}">Round {rd["n"]}</option>'
                      for i, rd in enumerate(replay["rounds"]))
    data_json = json.dumps(replay, separators=(",", ":"))
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
        '<span class="rtime" id="replay-time">0.0s</span>'
        '<span class="revt" id="replay-evt"></span>'
        '</div>'
        '<input type="range" id="replay-scrub" class="rscrub" min="0" max="0" value="0">'
        '<div class="replaywrap">'
        '<div class="radar-bg"></div>'
        f'<canvas id="replay-canvas" width="{size}" height="{size}"></canvas>'
        '</div>'
        f'<script type="application/json" id="replay-data">{data_json}</script>'
        '</div>'
    )


def render_html(summary, rounds, demo_name, meta) -> str:
    my, opp = summary["my_final"], summary["opp_final"]
    ref = meta["ref_name"]
    ref_sid = meta.get("ref_sid")
    info = maps.map_info(meta.get("map_name"))
    radar_uri = maps.radar_data_uri(info) if info else None

    parts = [PAGE_HEAD]
    if radar_uri:
        # Radar image embedded once; shared by the heatmap and replay panels.
        parts.append(f'<style>.radar-bg{{background-image:url("{radar_uri}");'
                     'background-size:cover;background-position:center}}</style>')
    parts.append(render_sticky(summary, rounds, meta))

    sub = (f'{esc(demo_name)} &nbsp;&middot;&nbsp; your player: '
           f'<span class="name me">{esc(ref)}</span>')
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

    if info:
        parts.append(render_map(summary.get("map_kills", []), meta, info))
        parts.append(render_replay(summary.get("replay"), meta, info))

    parts.append('<div class="rounds-title">Rounds</div>')
    parts.extend(render_round(rd, ref_sid) for rd in rounds)
    parts.append('<div class="empty">No rounds match this filter.</div>')

    parts.append(PAGE_FOOT)
    return "".join(parts)
