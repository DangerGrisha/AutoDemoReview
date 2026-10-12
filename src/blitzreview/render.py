"""HTML report for one Blitz battle -- self-contained (inline CSS and JS).

Every string that comes from a replay (nicknames, clan tags) is controlled by
whoever made the file and goes through `esc`; numbers are formatted, never
interpolated raw. The 2D replay's data goes into a JSON block through
`embed_json`, and its script only ever puts those strings on the page as text
(canvas text, textContent). The minimap is embedded as a data: URI, so a saved
page still renders. `site_nav` is trusted HTML built by the web layer.
"""

import base64
import html
import json
from datetime import datetime, timezone

from . import analysis, gamedata

OUTCOME = {
    "win": ("Победа", "win"),
    "loss": ("Поражение", "loss"),
    "draw": ("Ничья", "draw"),
    "unknown": ("Итог неизвестен", "unknown"),
}


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def num(value):
    if value is None:
        return "—"
    return f"{int(round(value)):,}".replace(",", " ")


def pct(value):
    return "—" if value is None else f"{value * 100:.0f}%"


def clock(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def embed_json(obj):
    """JSON for a <script type="application/json"> block. '<', '>' and '&'
    become \\u003c-style escapes -- still valid JSON that parses back
    identically -- so a nickname like '</script>' cannot end the block early."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def _when(ts):
    """A <time> the page script re-renders in the viewer's local time zone."""
    if not ts:
        return ""
    utc = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    return f'<time data-ts="{int(ts)}">{utc}</time>'


def _minimap_uri(key):
    path = gamedata.minimap_path(key)
    if path is None:
        return None
    return "data:image/webp;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _tank_kind(tank):
    kind = gamedata.class_name(tank.get("type")) or ""
    return f"{gamedata.tier_roman(tank.get('tier'))} {kind}".strip()


def _side_class(vehicle):
    if vehicle["is_author"]:
        return "m"
    return "a" if vehicle["ally"] else "e"


def _replay_geo(battle):
    """The minimap geometry when the battle has tracks to draw on it, else None."""
    if not (battle.get("replay") or {}).get("tracks"):
        return None
    return gamedata.minimap(battle["map"].get("key"))


def _seek(t):
    """A link target that makes the 2D replay jump to battle time `t`."""
    return f"#t={max(0, int(t))}"


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #

def _hero(battle):
    label, cls = OUTCOME.get(battle["outcome"], OUTCOME["unknown"])
    minimap = _minimap_uri(battle["map"].get("key"))
    img = (f'<img id="minimap-img" src="{minimap}" alt="Миникарта: {esc(battle["map"]["name"])}">'
           if minimap else "")
    bits = [esc(battle["mode"]["name"]) if battle.get("mode") else "режим неизвестен",
            _when(battle.get("started_at")),
            f"бой {clock(battle.get('duration'))}"]
    note = ""
    start, end, duration = (battle.get("battle_start_clock"),
                            battle.get("recording_end_clock"), battle.get("duration"))
    if start is not None and end is not None and duration and end - start < duration - 3:
        note = (f'<p class="note">Запись обрывается на {clock(end - start)} из '
                f'{clock(duration)}: игрок вышел из боя, дальше уничтожений в ленте нет.</p>')
    return (f'<section class="card hero">{img}<div>'
            f'<h1>{esc(battle["map"]["name"])}</h1>'
            f'<div class="meta">{" · ".join(b for b in bits if b)}</div>'
            f'<span class="badge {cls}">{label}</span>{note}</div></section>')


def _tile(label, value, sub="", rank=None):
    rank_html = ""
    top = ""
    if rank:
        rank_html = f'<div class="rk">#{rank["rank"]} из {rank["of"]}</div>'
        top = " top" if rank["rank"] == 1 else ""
    sub_html = f'<div class="s">{sub}</div>' if sub else ""
    return (f'<div class="tile{top}"><div class="l">{label}</div>'
            f'<div class="v">{value}</div>{sub_html}{rank_html}</div>')


def _fate(battle, me):
    names = {v["entity_id"]: v for v in battle["vehicles"]}
    stats = me.get("stats")
    if me["alive"]:
        if stats and stats.get("max_hp"):
            return f"Выжил: {num(stats['hp_left'])} HP из {num(stats['max_hp'])}."
        if stats:
            return f"Выжил: {num(stats['hp_left'])} HP."
        return "Жив на момент конца записи."
    if me["alive"] is None:
        return ""
    killer = names.get(me.get("killer_id"))
    when = ""
    if me.get("death_time") is not None:
        when = f" на {clock(me['death_time'])}"
        if _replay_geo(battle):
            when = f' <a class="t" href="{_seek(me["death_time"] - 5)}">на {clock(me["death_time"])}</a>'
    if killer:
        return (f'Уничтожен{when} — <span class="{_side_class(killer)}">'
                f'{esc(killer["nickname"])}</span> ({esc(killer["tank"]["short"])}).')
    return f"Уничтожен{when}."


def _me(battle):
    me = analysis.author(battle)
    if me is None:
        return ""
    tank = me["tank"]
    head = (f'<div class="me-head"><span class="me-tank">{esc(tank["name"])}</span>'
            f'<span class="muted">{esc(_tank_kind(tank))}'
            f'{" · премиум" if tank.get("premium") else ""}</span></div>')
    stats = me.get("stats")
    if stats is None:
        body = ('<p class="note">Итогов боя в реплее нет — игрок вышел до того, как они '
                'пришли. Ниже состав команд и уничтожения из записи.</p>')
        return (f'<section class="card"><h2>Ваш бой</h2>{head}{body}'
                f'<p class="fate">{_fate(battle, me)}</p></section>')

    ranks = analysis.lobby_ranks(battle)
    rates = analysis.rates(stats)
    totals = (battle.get("author") or {}).get("totals") or {}
    tiles = [
        _tile("Урон", num(stats["damage"]), rank=ranks.get("damage")),
        _tile("Фраги", num(stats["kills"]), rank=ranks.get("kills")),
        _tile("Точность", pct(rates["accuracy"]),
              f'{stats["hits"]} из {stats["shots"]} выстрелов', ranks.get("accuracy")),
        _tile("Пробития", pct(rates["pen_rate"]),
              f'{stats["penetrations"]} из {stats["hits"]} попаданий', ranks.get("pen_rate")),
        _tile("Урон за выстрел", num(rates["damage_per_shot"]),
              rank=ranks.get("damage_per_shot")),
        _tile("Помощь", num(stats["assist"]), "засвет и сбитые гусеницы",
              ranks.get("assist")),
        _tile("Заблокировано", num(stats["damage_blocked"]), rank=ranks.get("damage_blocked")),
        _tile("Получено урона", num(stats["damage_received"]),
              f'{stats["pens_received"]} пробитий из {stats["hits_received"]} попаданий'),
        _tile("Опыт", num(totals.get("xp", stats["xp"])), rank=ranks.get("xp")),
        _tile("Кредиты", num(totals.get("credits", stats["credits"]))),
    ]
    return (f'<section class="card"><h2>Ваш бой</h2>{head}'
            f'<div class="tiles">{"".join(tiles)}</div>'
            f'<p class="fate">{_fate(battle, me)}</p></section>')


def _hp_cell(vehicle, names):
    stats = vehicle.get("stats")
    killer = names.get(vehicle.get("killer_id"))
    title = f' title="Уничтожил: {esc(killer["nickname"])}"' if killer else ""
    if stats is None:
        if vehicle["alive"] is None:
            return '<td class="n">—</td>'
        if vehicle["alive"]:
            return '<td class="n">жив</td>'
        return f'<td class="n dead-mark"{title}>уничтожен</td>'
    if stats["hp_left"] <= 0:
        return f'<td class="n dead-mark"{title}>✕</td>'
    bar = ""
    if stats.get("max_hp"):
        width = max(2, min(100, round(100 * stats["hp_left"] / stats["max_hp"])))
        bar = f'<span class="hpbar"><i style="width:{width}%"></i></span>'
    return f'<td class="n"><span class="hp">{bar}{num(stats["hp_left"])}</span></td>'


def _team(battle, ally, leaders):
    side = [v for v in battle["vehicles"] if v["ally"] == ally]
    names = {v["entity_id"]: v for v in battle["vehicles"]}
    summary = analysis.team_summary(battle, ally)
    sums = []
    if summary["damage"] is not None:
        sums.append(f"урон {num(summary['damage'])}")
        sums.append(f"фраги {summary['kills']}")
    sums.append(f"в живых {summary['alive']}/{summary['players']}")
    if summary["avg_tier"]:
        sums.append(f"ср. уровень {summary['avg_tier']:.1f}")
    if summary["avg_rating"]:
        sums.append(f"рейтинг ≈{num(summary['avg_rating'])} (у {summary['ratings_known']} "
                    f"из {summary['players']})")

    def cell(vehicle, key, text):
        value = analysis.metric(vehicle, key)
        lead = value is not None and value == leaders.get(key) and value > 0
        return f'<td class="n{" lead" if lead else ""}">{text}</td>'

    rows = []
    for v in side:
        stats = v.get("stats")
        cls = " ".join(c for c in ("me" if v["is_author"] else "",
                                   "dead" if v["alive"] is False else "") if c)
        platoon = f'<span class="plt" title="Взвод">{v["platoon"]}</span>' if v.get("platoon") else ""
        clan = f' <span class="clan">[{esc(v["clan_tag"])}]</span>' if v.get("clan_tag") else ""
        tank = (f'{esc(v["tank"]["short"])}<span class="tier">'
                f'{esc(_tank_kind(v["tank"]))}</span>')
        if stats:
            shots = f'{stats["shots"]}/{stats["hits"]}/{stats["penetrations"]}'
            cells = (cell(v, "damage", num(stats["damage"]))
                     + cell(v, "kills", num(stats["kills"]))
                     + f'<td class="n">{shots}</td>'
                     + cell(v, "assist", num(stats["assist"]))
                     + cell(v, "damage_blocked", num(stats["damage_blocked"])))
        else:
            cells = '<td class="n">—</td>' * 5
        rows.append(f'<tr class="{cls}"><td>{platoon}{esc(v["nickname"])}{clan}</td>'
                    f'<td>{tank}</td>{cells}{_hp_cell(v, names)}</tr>')

    title, css = ("Союзники", "ally") if ally else ("Противники", "enemy")
    return (f'<section class="card {css}"><div class="side-head {css}"><h2>{title}</h2>'
            f'<span class="side-sum">{" · ".join(sums)}</span></div>'
            '<div class="tbl-wrap"><table><thead><tr><th>Игрок</th><th>Танк</th>'
            '<th class="n">Урон</th><th class="n">Фраги</th>'
            '<th class="n" title="Выстрелы / попадания / пробития">В/П/П</th>'
            '<th class="n">Помощь</th><th class="n">Блок</th><th class="n">HP</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')


def _feed_list(battle, linked=False):
    """The kill feed; `linked` times jump the 2D replay to a few seconds before."""
    names = {v["entity_id"]: v for v in battle["vehicles"]}
    items = []
    for kill in battle["kills"]:
        victim = names.get(kill["victim"])
        killer = names.get(kill["killer"])
        if victim is None:
            continue
        who = (f'<span class="{_side_class(killer)}">{esc(killer["nickname"])}</span> '
               f'<span class="tank">{esc(killer["tank"]["short"])}</span>'
               if killer else '<span class="muted">—</span>')
        if linked and kill["t"] is not None:
            when = f'<a class="t" href="{_seek(kill["t"] - 3)}">{clock(kill["t"])}</a>'
            attr = f' data-t="{kill["t"]:.1f}"'
        else:
            when, attr = f'<span class="t">{clock(kill["t"])}</span>', ""
        items.append(f'<li{attr}>{when}<span>{who} → '
                     f'<span class="{_side_class(victim)}">{esc(victim["nickname"])}</span> '
                     f'<span class="tank">{esc(victim["tank"]["short"])}</span></span></li>')
    return (f'<ul class="feed">{"".join(items)}</ul>' if items
            else '<p class="note">В записи уничтожений нет.</p>')


def _feed(battle):
    return f'<section class="card"><h2>Уничтожения</h2>{_feed_list(battle)}</section>'


def _replay_ticks(battle, end):
    """Kill marks above the scrubber: green -- an enemy fell, red -- an ally,
    gold -- the recorder killed or died."""
    names = {v["entity_id"]: v for v in battle["vehicles"]}
    marks = []
    for kill in battle["kills"]:
        victim, killer = names.get(kill["victim"]), names.get(kill["killer"])
        if victim is None or kill["t"] is None or end <= 0:
            continue
        if victim["is_author"] or (killer and killer["is_author"]):
            cls = "m"
        else:
            cls = "e" if victim["ally"] else "a"
        left = max(0.0, min(100.0, 100 * kill["t"] / end))
        marks.append(f'<i class="{cls}" style="left:{left:.2f}%" title="{clock(kill["t"])}"></i>')
    return "".join(marks)


def _replay_data(battle, geo):
    rp = battle["replay"]
    return {
        "end": rp["end"], "gap": rp["gap"],
        "world": geo["world"], "crop": geo["crop"], "border": geo["border"],
        "view": geo["view"],
        "vehicles": [{"id": v["entity_id"], "n": v["nickname"], "k": v["tank"]["short"],
                      "s": _side_class(v), "d": v["death_time"]} for v in battle["vehicles"]],
        "tracks": rp["tracks"],
        "kills": [{"t": k["t"], "v": k["victim"], "k": k["killer"]}
                  for k in battle["kills"] if k["t"] is not None],
    }


SPEEDS = (1, 2, 4, 8, 16)
DEFAULT_SPEED = 4


def _seg(group, options, active):
    buttons = []
    for value, label in options:
        on = ' class="on"' if value == active else ""
        buttons.append(f'<button type="button" data-v="{value}"{on}>{label}</button>')
    return f'<div class="seg" data-group="{group}">{"".join(buttons)}</div>'


def _replay(battle):
    """The 2D minimap replay card with the kill feed beside it; "" when the map
    has no minimap, a short note when the battle has no tracks."""
    if gamedata.minimap(battle["map"].get("key")) is None:
        return ""
    geo = _replay_geo(battle)
    if geo is None:
        stored_before_tracks = (battle.get("format") or 1) < 2  # tracks came with format 2
        why = ("В этой записи боя ещё нет треков: загрузите реплей заново, чтобы "
               "увидеть 2D-повтор." if stored_before_tracks
               else "В реплее нет позиций танков — 2D-повтор недоступен.")
        return f'<section class="card"><h2>2D-повтор</h2><p class="note">{why}</p></section>'
    end = battle["replay"]["end"]
    controls = (
        '<button type="button" class="rbtn play" id="rp-play" title="Смотреть (пробел)">▶</button>'
        f'<span class="rtime" id="rp-time">0:00 / {clock(end)}</span>'
        + _seg("speed", [(v, f"{v}×") for v in SPEEDS], DEFAULT_SPEED)
        + '<span class="rp-opt">след'
        + _seg("trail", [(0, "нет"), (20, "20 с"), (-1, "весь путь")], 20) + '</span>'
        + '<span class="rp-opt">подписи'
        + _seg("label", [("tank", "танки"), ("nick", "ники"), ("none", "нет")], "tank")
        + '</span>')
    legend = ('<div class="rp-legend"><span><i class="lm"></i>вы</span>'
              '<span><i class="la"></i>союзники</span><span><i class="le"></i>противники</span>'
              '<span><i class="lg"></i>последний раз видели</span><span>✕ уничтожен</span></div>')
    return (
        '<section class="card replay" id="replay">'
        '<div class="rp-head"><h2>2D-повтор</h2><span class="note">Противники видны, пока их '
        'засвечивают; потом — пунктир там, где их видели в последний раз.</span></div>'
        '<div class="rp-grid"><div class="rp-main">'
        '<div class="rp-stage" id="rp-stage" tabindex="0">'
        f'<canvas id="rp-canvas" aria-label="2D-повтор боя на миникарте: '
        f'{esc(battle["map"]["name"])}"></canvas><div class="rp-recent" id="rp-recent"></div></div>'
        f'<div class="rp-timeline"><div class="rp-ticks">{_replay_ticks(battle, end)}</div>'
        f'<input type="range" id="rp-scrub" min="0" max="{end:.1f}" step="0.1" value="0" '
        'aria-label="Время боя"></div>'
        f'<div class="rp-controls">{controls}</div></div>'
        f'<aside class="rp-side"><h3>Уничтожения</h3>{_feed_list(battle, linked=True)}{legend}'
        '<p class="note">Пробел — пауза, ← → — на 5 секунд.</p></aside></div>'
        f'<script type="application/json" id="replay-data">{embed_json(_replay_data(battle, geo))}'
        '</script></section>')


def _footer(battle):
    bits = [f"клиент {esc(battle.get('client_version'))}"]
    if battle.get("arena_id"):
        bits.append(f"бой #{esc(battle['arena_id'])}")
    tech = ""
    if battle.get("warnings"):
        tech = ('<details class="tech"><summary>Технические заметки</summary><ul>'
                + "".join(f"<li>{esc(w)}</li>" for w in battle["warnings"])
                + "</ul></details>")
    return f'{tech}<footer>{" · ".join(bits)}</footer>'


# --------------------------------------------------------------------------- #
# page
# --------------------------------------------------------------------------- #

_CSS = """
:root{--bg:#0B0E11;--panel:#12161B;--panel2:#161B21;--line:#232A31;--text:#E4E7EA;
--muted:#8A929B;--ally:#7BBE7B;--enemy:#D86A5A;--me:#E8C25A;
--display:"Rajdhani","Oswald","Segoe UI",system-ui,sans-serif;
--body:"Inter",-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
--mono:"JetBrains Mono",ui-monospace,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--body)}
.wrap{max-width:1180px;margin:0 auto;padding:12px 16px 24px}
.muted{color:var(--muted)}
.site-nav{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:10px 16px;
border-bottom:1px solid var(--line);background:var(--panel)}
.site-nav a{color:var(--text);text-decoration:none;font-weight:600}
.site-nav a:hover{color:var(--me)}
.site-nav .sn-brand{font:700 18px var(--display);letter-spacing:1px;margin-left:auto}
.site-nav .sn-user{display:flex;align-items:center;gap:8px;color:var(--muted)}
.site-nav .sn-user img{width:24px;height:24px;border-radius:50%}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin:14px 0}
.hero{display:flex;gap:18px;align-items:center}
.hero img{width:132px;height:132px;border-radius:8px;border:1px solid var(--line);flex:none}
.hero h1{font:700 36px/1.05 var(--display);margin:0;letter-spacing:.5px}
.meta{color:var(--muted);margin-top:6px}
.badge{display:inline-block;font:700 15px var(--display);letter-spacing:1px;
text-transform:uppercase;padding:4px 12px;border-radius:6px;margin-top:10px;
border:1px solid var(--line);color:var(--muted)}
.badge.win{color:#8fd18f;border-color:rgba(123,190,123,.55);background:rgba(123,190,123,.12)}
.badge.loss{color:#ef8b7d;border-color:rgba(216,106,90,.55);background:rgba(216,106,90,.12)}
.note{color:var(--muted);font-size:13px;margin:8px 0 0}
h2{font:700 20px var(--display);letter-spacing:.6px;margin:0 0 12px;text-transform:uppercase}
.me-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;margin-bottom:12px}
.me-tank{font:700 24px var(--display)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
.tile{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.tile .l{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.tile .v{font:700 26px/1.15 var(--display);margin-top:2px}
.tile .s{color:var(--muted);font-size:12px}
.tile .rk{font:600 12px var(--mono);color:var(--muted);margin-top:2px}
.tile.top{border-color:rgba(232,194,90,.55)}
.tile.top .v,.tile.top .rk{color:var(--me)}
.fate{margin:14px 0 0}
.side-head{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;
gap:8px;margin-bottom:8px}
.side-head h2{margin:0}
.side-head.ally h2{color:var(--ally)}
.side-head.enemy h2{color:var(--enemy)}
.side-sum{color:var(--muted);font:12px var(--mono)}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.4px;text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
th.n,td.n{text-align:right;font-family:var(--mono)}
tr.me td{background:rgba(232,194,90,.08)}
tr.me td:first-child{box-shadow:inset 3px 0 var(--me)}
tr.dead td{color:var(--muted)}
td.lead{color:var(--me);font-weight:700}
.clan{color:var(--muted);font-size:12px}
.plt{display:inline-block;min-width:18px;padding:0 4px;margin-right:6px;border-radius:4px;
background:var(--line);font:600 11px/16px var(--mono);text-align:center;color:var(--text)}
.tier{color:var(--muted);font-size:11px;margin-left:6px}
.hp{display:inline-flex;align-items:center;gap:6px}
.hpbar{width:54px;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.hpbar i{display:block;height:100%;background:var(--ally)}
.enemy .hpbar i{background:var(--enemy)}
.dead-mark{color:var(--muted)}
.feed{list-style:none;margin:0;padding:0}
.feed li{display:flex;gap:12px;padding:6px 0;border-bottom:1px solid var(--line)}
.feed li:last-child{border-bottom:none}
.feed .t{font-family:var(--mono);color:var(--muted);width:44px;flex:none}
.feed .tank{color:var(--muted);font-size:12px}
.a{color:var(--ally)}.e{color:var(--enemy)}.m{color:var(--me);font-weight:700}
a.t{color:var(--muted);text-decoration:underline dotted;text-underline-offset:3px}
a.t:hover{color:var(--me)}
.feed a.t{align-self:flex-start}
.fate a.t{color:inherit}
.rp-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;
gap:4px 16px;margin-bottom:12px}
.rp-head h2{margin:0}.rp-head .note{margin:0}
.rp-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:18px;align-items:start}
.rp-main{min-width:0;max-width:720px}
.rp-stage{position:relative;width:100%;aspect-ratio:1/1;border-radius:8px;overflow:hidden;
border:1px solid var(--line);background:#06080A;outline:none;cursor:pointer}
.rp-stage:focus-visible{border-color:var(--me)}
.rp-stage canvas{position:absolute;inset:0;width:100%;height:100%;display:block}
.rp-recent{position:absolute;top:8px;right:8px;display:flex;flex-direction:column;gap:3px;
align-items:flex-end;pointer-events:none}
.rp-recent div{font-size:12px;font-weight:600;background:rgba(11,14,17,.8);
border:1px solid var(--line);border-radius:6px;padding:2px 7px;white-space:nowrap}
.rp-timeline{margin-top:10px}
.rp-ticks{position:relative;height:8px;margin:0 7px}
.rp-ticks i{position:absolute;top:0;width:3px;height:8px;margin-left:-1px;border-radius:1px;
background:var(--muted)}
.rp-ticks i.a{background:var(--ally)}.rp-ticks i.e{background:var(--enemy)}
.rp-ticks i.m{background:var(--me)}
#rp-scrub{width:100%;margin:2px 0 0;accent-color:var(--me)}
.rp-controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.rbtn{font:inherit;font-size:13px;color:var(--text);background:var(--panel2);
border:1px solid var(--line);border-radius:8px;padding:5px 12px;cursor:pointer;min-width:40px}
.rbtn:hover{border-color:var(--me)}
.rbtn.play{background:var(--me);border-color:var(--me);color:#0B0E11;font-weight:700}
.rtime{font:13px var(--mono);color:var(--muted);min-width:92px}
.rp-opt{display:inline-flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}
.seg{display:inline-flex;background:var(--panel2);border:1px solid var(--line);
border-radius:8px;overflow:hidden}
.seg button{font:inherit;font-size:12px;color:var(--muted);background:transparent;border:0;
padding:5px 9px;cursor:pointer}
.seg button+button{border-left:1px solid var(--line)}
.seg button.on{background:var(--me);color:#0B0E11;font-weight:600}
.rp-side h3{font:700 16px var(--display);letter-spacing:.5px;text-transform:uppercase;
margin:0 0 6px}
.rp-side .feed{max-height:560px;overflow-y:auto;font-size:13px}
.rp-side .feed li{opacity:.45;transition:opacity .2s}
.rp-side .feed li.past{opacity:1}
.rp-side .feed li.now{background:rgba(232,194,90,.08)}
.rp-legend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:12px;color:var(--muted);
font-size:12px}
.rp-legend span{display:inline-flex;align-items:center;gap:6px}
.rp-legend i{width:10px;height:10px;border-radius:50%;display:inline-block}
.rp-legend .la{background:var(--ally)}.rp-legend .le{background:var(--enemy)}
.rp-legend .lm{background:var(--me)}
.rp-legend .lg{border:1.5px dashed var(--enemy)}
@media(max-width:900px){.rp-grid{grid-template-columns:minmax(0,1fr)}
.rp-side .feed{max-height:none}}
details.tech{color:var(--muted);font-size:12px;margin:18px 0 6px}
footer{color:var(--muted);font-size:12px;padding:6px 0 0}
@media(max-width:640px){.hero{flex-direction:column;align-items:flex-start}
.hero img{width:96px;height:96px}.hero h1{font-size:30px}}
"""

_SCRIPT = """
document.querySelectorAll('time[data-ts]').forEach(function (el) {
  var d = new Date(+el.dataset.ts * 1000);
  el.textContent = d.toLocaleString('ru-RU', {day: '2-digit', month: '2-digit',
    year: 'numeric', hour: '2-digit', minute: '2-digit'});
});
"""

# The 2D replay. Tracks are decoded once (see replay.py for the delta coding),
# then every frame draws: the minimap cropped to the view, the outside of the
# playable border dimmed, trails, wrecks, "last seen" rings, live tanks (hull
# heading as the nose), labels, and a ring where somebody just died. Strings
# from the replay reach the page only through fillText and textContent.
_REPLAY_SCRIPT = r"""
(function () {
  var dataEl = document.getElementById('replay-data');
  var img = document.getElementById('minimap-img');
  if (!dataEl || !img) return;
  var R = JSON.parse(dataEl.textContent);
  var card = document.getElementById('replay');
  var stage = document.getElementById('rp-stage');
  var cv = document.getElementById('rp-canvas');
  var ctx = cv.getContext('2d');
  var scrub = document.getElementById('rp-scrub');
  var playBtn = document.getElementById('rp-play');
  var timeEl = document.getElementById('rp-time');
  var recentEl = document.getElementById('rp-recent');
  var feed = Array.prototype.slice.call(card.querySelectorAll('.feed li[data-t]'));
  var COL = {m: '#E8C25A', a: '#7BBE7B', e: '#D86A5A'};
  var TEXT = {m: '#F6DE94', a: '#BFE6BF', e: '#F4AEA2'};
  var END = R.end, GAP = R.gap, TAU = Math.PI * 2, NOSE = Math.acos(1 / 1.75);
  function chosen(group) {  // the defaults are whichever buttons the page marked "on"
    var b = card.querySelector('.seg[data-group="' + group + '"] button.on');
    return b ? b.getAttribute('data-v') : null;
  }
  var speed = +chosen('speed') || 4, trail = +chosen('trail'), labels = chosen('label') || 'tank';
  var cur = 0, playing = false, lastTs = 0, W = 1, H = 1, dpr = 1, recentSig = '';

  var byId = {}, V = [];
  R.vehicles.forEach(function (v) { byId[v.id] = v; });
  R.tracks.forEach(function (tr) {
    var v = byId[tr.id];
    if (!v) return;
    var p = tr.p, n = Math.floor(p.length / 4);
    var T = new Float64Array(n), X = new Float64Array(n), Z = new Float64Array(n),
        Y = new Float64Array(n), t = 0, x = 0, z = 0, y = 0;
    for (var i = 0; i < n; i++) {
      var k = 4 * i;
      if (i === 0) { t = p[0]; x = p[1]; z = p[2]; y = p[3]; }
      else { t += p[k]; x += p[k + 1]; z += p[k + 2]; y += p[k + 3]; }
      T[i] = t / 10; X[i] = x / 10; Z[i] = z / 10; Y[i] = y;  // yaw unwrapped: lerp-safe
    }
    v.T = T; v.X = X; v.Z = Z; v.Y = Y;
    V.push(v);
  });
  var ORDER = {e: 0, a: 1, m: 2};  // the recorder is drawn last, on top
  V.sort(function (a, b) { return ORDER[a.s] - ORDER[b.s]; });

  var view = R.view;
  function px(x) { return (x - view[0]) / (view[2] - view[0]) * W; }
  function py(z) { return (view[3] - z) / (view[3] - view[1]) * H; }
  function ix(x) { return R.crop[0] + (x - R.world[0]) / (R.world[2] - R.world[0]) * (R.crop[2] - R.crop[0]); }
  function iy(z) { return R.crop[1] + (R.world[3] - z) / (R.world[3] - R.world[1]) * (R.crop[3] - R.crop[1]); }

  function at(T, t) {  // index of the last sample at or before t, -1 if none
    var lo = 0, hi = T.length - 1, found = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (T[mid] <= t) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return found;
  }
  function state(v, t) {
    var T = v.T, n = T.length;
    if (v.d !== null && t >= v.d) {
      var j = at(T, v.d + 0.25);
      return j < 0 ? null : {k: 'dead', x: v.X[j], z: v.Z[j], y: v.Y[j]};
    }
    var i = at(T, t);
    if (i < 0) return null;
    if (i + 1 < n && T[i + 1] - T[i] <= GAP) {
      var f = (t - T[i]) / (T[i + 1] - T[i]);
      return {k: 'live', x: v.X[i] + (v.X[i + 1] - v.X[i]) * f,
              z: v.Z[i] + (v.Z[i + 1] - v.Z[i]) * f, y: v.Y[i] + (v.Y[i + 1] - v.Y[i]) * f};
    }
    var age = t - T[i];
    if (age <= GAP) return {k: 'live', x: v.X[i], z: v.Z[i], y: v.Y[i]};
    return {k: 'ghost', x: v.X[i], z: v.Z[i], y: v.Y[i], age: age};
  }

  function drawMap() {
    if (img.complete && img.naturalWidth) {
      var sx = ix(view[0]), sy = iy(view[3]);
      ctx.drawImage(img, sx, sy, ix(view[2]) - sx, iy(view[1]) - sy, 0, 0, W, H);
    }
    if (!R.border) return;
    var b = R.border, bx = px(b[0]), by = py(b[3]), bw = px(b[2]) - bx, bh = py(b[1]) - by;
    ctx.save();
    ctx.fillStyle = 'rgba(6,8,10,.5)';
    ctx.beginPath(); ctx.rect(0, 0, W, H); ctx.rect(bx, by, bw, bh); ctx.fill('evenodd');
    ctx.strokeStyle = 'rgba(255,255,255,.3)'; ctx.lineWidth = dpr;
    ctx.strokeRect(bx, by, bw, bh);
    ctx.restore();
  }
  function drawTrail(v, s, t) {
    if (!trail) return;
    var T = v.T, stop = s && s.k === 'dead' ? at(T, v.d + 0.25) : at(T, t);
    var j = trail > 0 ? Math.max(0, at(T, t - trail)) : 0;
    if (stop < 0 || stop <= j && !(s && s.k === 'live')) return;
    ctx.save();
    ctx.strokeStyle = COL[v.s]; ctx.globalAlpha = v.s === 'm' ? .9 : .55;
    ctx.lineWidth = (v.s === 'm' ? 2.2 : 1.5) * dpr; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.beginPath();
    var pen = false;
    for (var k = j; k <= stop; k++) {
      if (k > j && T[k] - T[k - 1] > GAP) pen = false;  // unseen stretch: no line
      if (pen) ctx.lineTo(px(v.X[k]), py(v.Z[k]));
      else { ctx.moveTo(px(v.X[k]), py(v.Z[k])); pen = true; }
    }
    if (pen && s && s.k === 'live') ctx.lineTo(px(s.x), py(s.z));
    ctx.stroke();
    ctx.restore();
  }
  function cross(r) {
    ctx.beginPath();
    ctx.moveTo(-r, -r); ctx.lineTo(r, r); ctx.moveTo(r, -r); ctx.lineTo(-r, r);
    ctx.stroke();
  }
  function drawTank(v, s) {
    var r = (v.s === 'm' ? 6.5 : 5.5) * dpr, c = COL[v.s];
    ctx.save();
    ctx.translate(px(s.x), py(s.z));
    if (s.k === 'dead') {
      ctx.lineCap = 'round';
      ctx.strokeStyle = 'rgba(0,0,0,.65)'; ctx.lineWidth = 4.5 * dpr; cross(r * .8);
      ctx.strokeStyle = c; ctx.lineWidth = 2.2 * dpr; cross(r * .8);
    } else if (s.k === 'ghost') {
      ctx.globalAlpha = Math.max(.3, .85 - s.age / 60);
      ctx.setLineDash([2.5 * dpr, 2 * dpr]);
      ctx.strokeStyle = c; ctx.lineWidth = 1.6 * dpr;
      ctx.beginPath(); ctx.arc(0, 0, r, 0, TAU); ctx.stroke();
    } else {
      ctx.rotate(s.y * Math.PI / 180);  // 0 = north, clockwise -- canvas rotates clockwise too
      ctx.beginPath();
      ctx.moveTo(0, -1.75 * r);
      ctx.arc(0, 0, r, -Math.PI / 2 + NOSE, -Math.PI / 2 - NOSE + TAU);
      ctx.closePath();
      ctx.fillStyle = c; ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,.75)'; ctx.lineWidth = 1.4 * dpr; ctx.stroke();
      if (v.s === 'm') {
        ctx.beginPath(); ctx.arc(0, 0, r + 3 * dpr, 0, TAU);
        ctx.strokeStyle = 'rgba(255,255,255,.9)'; ctx.lineWidth = 1.5 * dpr; ctx.stroke();
      }
    }
    ctx.restore();
  }
  function drawLabel(v, s) {
    if (labels === 'none') return;
    var text = labels === 'nick' ? v.n : v.k;
    if (s.k === 'ghost') text += ' · ' + Math.round(s.age) + ' с';
    var x = px(s.x), y = py(s.z) + (s.k === 'dead' ? 7 : 10) * dpr;
    ctx.save();
    ctx.font = '600 ' + Math.round(11 * dpr) + 'px Inter, "Segoe UI", Roboto, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.lineJoin = 'round';
    ctx.globalAlpha = s.k === 'live' ? 1 : .65;
    ctx.lineWidth = 3 * dpr; ctx.strokeStyle = 'rgba(0,0,0,.85)';
    ctx.strokeText(text, x, y);
    ctx.fillStyle = s.k === 'dead' ? '#A2AAB3' : TEXT[v.s];
    ctx.fillText(text, x, y);
    ctx.restore();
  }
  function drawFlashes(t) {
    R.kills.forEach(function (k) {
      var age = t - k.t, v = byId[k.v];
      if (age < 0 || age > 1.5 || !v || !v.T) return;
      var s = state(v, k.t + 0.05);
      if (!s) return;
      ctx.save();
      ctx.globalAlpha = 1 - age / 1.5; ctx.strokeStyle = '#FFFFFF'; ctx.lineWidth = 2 * dpr;
      ctx.beginPath(); ctx.arc(px(s.x), py(s.z), (8 + age * 22) * dpr, 0, TAU); ctx.stroke();
      ctx.restore();
    });
  }

  function render() {
    ctx.clearRect(0, 0, W, H);
    drawMap();
    var states = V.map(function (v) { return state(v, cur); });
    V.forEach(function (v, i) { drawTrail(v, states[i], cur); });
    ['dead', 'ghost', 'live'].forEach(function (kind) {
      V.forEach(function (v, i) { if (states[i] && states[i].k === kind) drawTank(v, states[i]); });
    });
    V.forEach(function (v, i) { if (states[i]) drawLabel(v, states[i]); });
    drawFlashes(cur);
  }

  function fmt(t) {
    t = Math.max(0, Math.floor(t));
    return Math.floor(t / 60) + ':' + ('0' + t % 60).slice(-2);
  }
  function who(v) {
    var span = document.createElement('span');
    span.className = v ? v.s : 'muted';
    span.textContent = v ? (labels === 'nick' ? v.n : v.k) : '—';
    return span;
  }
  function showRecent(t) {
    var list = R.kills.filter(function (k) { return k.t <= t && t - k.t <= 12; }).slice(-5).reverse();
    var sig = labels + list.map(function (k) { return k.t + ':' + k.v; }).join('|');
    if (sig === recentSig) return;
    recentSig = sig;
    recentEl.textContent = '';
    list.forEach(function (k) {
      var row = document.createElement('div');
      row.appendChild(who(byId[k.k]));
      row.appendChild(document.createTextNode(' ✕ '));
      row.appendChild(who(byId[k.v]));
      recentEl.appendChild(row);
    });
  }
  function draw() {
    render();
    scrub.value = cur.toFixed(1);
    timeEl.textContent = fmt(cur) + ' / ' + fmt(END);
    feed.forEach(function (li) {
      var t = +li.getAttribute('data-t');
      li.classList.toggle('past', t <= cur);
      li.classList.toggle('now', t <= cur && cur - t <= 4);
    });
    showRecent(cur);
  }

  function setPlay() {
    playBtn.textContent = playing ? '❚❚' : '▶';
    playBtn.title = playing ? 'Пауза (пробел)' : 'Смотреть (пробел)';
  }
  function tick(ts) {
    if (!playing) return;
    if (lastTs) cur = Math.min(END, cur + (ts - lastTs) / 1000 * speed);
    lastTs = ts;
    if (cur >= END) { playing = false; setPlay(); }
    draw();
    if (playing) requestAnimationFrame(tick);
  }
  function play(on) {
    if (on && cur >= END) cur = 0;
    playing = on; lastTs = 0; setPlay();
    if (on) requestAnimationFrame(tick); else draw();
  }
  function seek(t) { cur = Math.max(0, Math.min(END, t)); draw(); }

  playBtn.addEventListener('click', function () { play(!playing); });
  stage.addEventListener('click', function () { play(!playing); });
  scrub.addEventListener('input', function () { seek(+scrub.value); });
  Array.prototype.forEach.call(card.querySelectorAll('.seg'), function (seg) {
    seg.addEventListener('click', function (e) {
      var b = e.target.closest('button');
      if (!b) return;
      Array.prototype.forEach.call(seg.querySelectorAll('button'), function (x) {
        x.classList.toggle('on', x === b);
      });
      var g = seg.getAttribute('data-group'), value = b.getAttribute('data-v');
      if (g === 'speed') speed = +value;
      else if (g === 'trail') trail = +value;
      else labels = value;
      draw();
    });
  });
  card.addEventListener('keydown', function (e) {  // also over the slider: 0.1 s steps are too fine
    if (e.key === ' ') { e.preventDefault(); play(!playing); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); seek(cur - 5); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); seek(cur + 5); }
  });

  function fromHash(scroll) {
    var m = /^#t=(\d+(?:\.\d+)?)$/.exec(location.hash);
    if (!m) return false;
    if (playing) play(false);
    seek(+m[1]);
    if (scroll) card.scrollIntoView({behavior: 'smooth', block: 'start'});
    return true;
  }
  document.addEventListener('click', function (e) {  // a second click on the same link too
    var a = e.target.closest && e.target.closest('a[href^="#t="]');
    if (!a) return;
    e.preventDefault();
    history.replaceState(null, '', a.getAttribute('href'));
    fromHash(!card.contains(a));
  });
  window.addEventListener('hashchange', function () { fromHash(true); });

  function fit() {
    dpr = window.devicePixelRatio || 1;
    var w = Math.max(1, Math.round(stage.clientWidth * dpr));
    if (cv.width !== w) { cv.width = w; cv.height = w; }
    W = cv.width; H = cv.height;
    draw();
  }
  window.addEventListener('resize', fit);
  if (img.complete && img.naturalWidth) { fit(); fromHash(true); }
  else {
    fit();
    img.addEventListener('load', function () { fit(); fromHash(true); });
  }
})();
"""


def render_battle_html(battle, site_nav=""):
    """The full report page for one battle dict (see battle.load)."""
    label, _ = OUTCOME.get(battle["outcome"], OUTCOME["unknown"])
    title = f'{battle["map"]["name"]} — {label} · Blitz · demoReview'
    leaders = analysis.leaders(battle)
    has_replay = _replay_geo(battle) is not None  # the replay card carries the kill feed
    body = (_hero(battle) + _me(battle) + _replay(battle)
            + f'<div class="teams">{_team(battle, True, leaders)}{_team(battle, False, leaders)}</div>'
            + ("" if has_replay else _feed(battle)) + _footer(battle))
    script = _SCRIPT + (_REPLAY_SCRIPT if has_replay else "")
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;'
        '600;700&family=JetBrains+Mono:wght@400;600&family=Rajdhani:wght@600;700&display=swap">'
        f'<style>{_CSS}</style></head><body>{site_nav}'
        f'<main class="wrap">{body}</main><script>{script}</script></body></html>'
    )
