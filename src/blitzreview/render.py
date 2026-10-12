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


def _advantage(battle, end):
    """A strip under the timeline: allies minus enemies still in the battle,
    above the line while the allies have more tanks, below while the enemy
    does. Hovering a stretch shows the count."""
    if end <= 0:
        return ""
    allies = sum(1 for v in battle["vehicles"] if v["ally"])
    enemies = len(battle["vehicles"]) - allies
    deaths = sorted((v["death_time"], v["ally"]) for v in battle["vehicles"]
                    if v["death_time"] is not None and v["death_time"] < end)
    stretches, start = [], 0.0
    for t, ally in deaths:
        if t > start:
            stretches.append((start, t, allies, enemies))
            start = t
        allies, enemies = allies - ally, enemies - (not ally)
    stretches.append((start, end, allies, enemies))
    peak = max([3] + [abs(a - e) for _, _, a, e in stretches])
    parts = []
    for t0, t1, a, e in stretches:
        bar = ""
        if a != e:
            h = 9 * abs(a - e) / peak
            bar = (f'<rect class="{"a" if a > e else "e"}" x="{t0:.1f}" '
                   f'y="{10 - h if a > e else 10:.2f}" width="{t1 - t0:.1f}" height="{h:.2f}"/>')
        parts.append(f'<g><title>{clock(t0)}–{clock(t1)}: в строю {a} : {e}</title>'
                     f'<rect class="h" x="{t0:.1f}" y="0" width="{t1 - t0:.1f}" height="20"/>{bar}</g>')
    return (f'<svg class="rp-adv" viewBox="0 0 {end:.1f} 20" preserveAspectRatio="none" '
            'role="img" aria-label="Перевес по танкам в строю">'
            f'<line x1="0" y1="10" x2="{end:.1f}" y2="10" vector-effect="non-scaling-stroke"/>'
            f'{"".join(parts)}</svg>')


def _replay_vehicle(v):
    """What the replay script knows about one vehicle; `r` only with results."""
    out = {"id": v["entity_id"], "n": v["nickname"], "k": v["tank"]["short"],
           "s": _side_class(v), "d": v["death_time"], "c": v["tank"].get("type"),
           "tn": v["tank"]["name"], "tk": _tank_kind(v["tank"]), "cl": v.get("clan_tag"),
           "kid": v.get("killer_id")}
    stats = v.get("stats")
    if stats:
        out["r"] = {"dmg": stats["damage"], "fr": stats["kills"], "as": stats["assist"],
                    "bl": stats["damage_blocked"], "hp": stats["hp_left"],
                    "mhp": stats.get("max_hp")}
    return out


def _team_side(battle, team):
    """'a' for the recorder's team, 'e' for the other one, '' for nobody."""
    mine = (battle.get("author") or {}).get("team")
    if not team or mine is None:
        return ""
    return "a" if team == mine else "e"


def _replay_objectives(battle):
    """The capture zones of the battle's mode and what happened on them, in
    sides instead of team numbers; None when the map or the mode has none.
    Battles stored before format 3 get the zones without their history."""
    mode = (battle.get("mode") or {}).get("id")
    zones = gamedata.objectives(battle["map"].get("key"), mode)
    if not zones:
        return None
    stored = battle.get("objectives") or {}

    def side(team):
        return _team_side(battle, team)

    out = {"kind": "points" if mode == 1 else "base",
           "zones": [{"id": z["id"], "l": z["label"], "x": z["x"], "z": z["z"], "r": z["r"]}
                     for z in zones]}
    if mode == 1:
        out["states"] = [[t, i, side(owner), side(capturer), progress]
                         for t, i, owner, capturer, progress in stored.get("points") or ()]
        score, now = [], {}
        for t, team, points in stored.get("score") or ():
            now[side(team)] = points
            row = [t, now.get("a"), now.get("e")]
            if score and score[-1][0] == t:
                score[-1] = row  # both teams arrive in the same instant
            else:
                score.append(row)
        out["score"] = score
        out["goal"] = stored.get("goal")
    else:
        out["base"] = [[t, side(team), points] for t, team, points in stored.get("base") or ()]
    return out


def _best_ally(battle):
    """Entity id of the recorder's teammate (or the recorder) with the most
    damage, None without results."""
    allies = [v for v in battle["vehicles"] if v["ally"] and v.get("stats")]
    best = max(allies, key=lambda v: v["stats"]["damage"], default=None)
    return best["entity_id"] if best and best["stats"]["damage"] > 0 else None


def _replay_data(battle, geo):
    rp = battle["replay"]
    return {
        "end": rp["end"], "gap": rp["gap"],
        "world": geo["world"], "crop": geo["crop"], "border": geo["border"],
        "view": geo["view"],
        "vehicles": [_replay_vehicle(v) for v in battle["vehicles"]],
        "tracks": rp["tracks"],
        "kills": [{"t": k["t"], "v": k["victim"], "k": k["killer"]}
                  for k in battle["kills"] if k["t"] is not None],
        "obj": _replay_objectives(battle),
    }


def _replay_hud(battle, obj):
    """The board in the stage's corner: tanks still in the battle per side,
    and the Supremacy score or the Encounter capture."""
    allies = sum(1 for v in battle["vehicles"] if v["ally"])
    rows = [f'<div title="Танков в строю: союзники : противники"><span class="lbl">в строю</span>'
            f'<b class="a" id="rp-na">{allies}</b>:<b class="e" id="rp-ne">'
            f'{len(battle["vehicles"]) - allies}</b></div>']
    if obj and obj.get("score"):
        goal = f" (победа — {num(obj['goal'])})" if obj.get("goal") else ""
        rows.append(f'<div id="rp-score" title="Очки победы: союзники : противники{goal}">'
                    '<span class="lbl">очки</span><b class="a"></b>:<b class="e"></b></div>')
    if obj and obj.get("base"):
        rows.append('<div id="rp-cap" title="Захват базы: шкала до 100" hidden>'
                    '<span class="lbl">захват</span><b></b></div>')
    return f'<div class="rp-hud">{"".join(rows)}</div>'


def _focus_panel(battle):
    best = _best_ally(battle)
    button = (f'<button type="button" class="rbtn" id="rp-best" data-id="{best}">'
              '★ Лучший в команде</button>' if best else "")
    return ('<div class="rp-focus"><div id="rp-hint"><p class="note">Нажмите на танк на '
            f'карте — покажу его путь и итоги боя.</p>{button}</div>'
            '<div class="rp-card" id="rp-card" hidden></div></div>')


# The game's minimap class marks (BattleScreenHUD/MinimapTank): a diamond for
# tanks -- plain for LT, cut once for MT, twice for HT -- and a triangle
# pointing down for tank destroyers. The script draws the same shapes.
_MARKS = {
    "LT": '<path d="M0-6 6 0 0 6-6 0z"/>',
    "MT": '<path d="M0-6 6 0 0 6-6 0z"/><path class="cut" d="M3-3-3 3"/>',
    "HT": '<path d="M0-6 6 0 0 6-6 0z"/><path class="cut" d="M4-2-2 4M2-4-4 2"/>',
    "TD": '<path d="M-6.5-4.6H6.5L0 6.5z"/>',
}
_FLAG = '<path class="pole" d="M-2.5 6.5V-6.5"/><path d="M-2.5-6.5 5-3.5-2.5-.5z"/>'


def _mark(svg_body):
    return f'<svg viewBox="-7 -7 14 14" aria-hidden="true">{svg_body}</svg>'


def _legend(obj):
    classes = "".join(f'<span>{_mark(_MARKS[code])}{gamedata.class_name(code)}</span>'
                      for code in ("LT", "MT", "HT", "TD"))
    zones = ""
    if obj and obj["kind"] == "points":
        zones = '<span><b class="lp">A</b>точки захвата</span>'
    elif obj:
        zones = f'<span>{_mark(_FLAG)}база</span>'
    return ('<div class="rp-legend"><span><i class="lm"></i>вы</span>'
            '<span><i class="la"></i>союзники</span><span><i class="le"></i>противники</span>'
            f'{classes}<span><i class="lg"></i>последний раз видели</span><span>✕ уничтожен</span>'
            f'<span>→ кто кого уничтожил</span>{zones}</div>')


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
    data = _replay_data(battle, geo)
    controls = (
        '<button type="button" class="rbtn play" id="rp-play" title="Смотреть (пробел)">▶</button>'
        f'<span class="rtime" id="rp-time">0:00 / {clock(end)}</span>'
        + _seg("speed", [(v, f"{v}×") for v in SPEEDS], DEFAULT_SPEED)
        + '<span class="rp-opt">след'
        + _seg("trail", [(0, "нет"), (20, "20 с"), (-1, "весь путь")], 20) + '</span>'
        + '<span class="rp-opt">подписи'
        + _seg("label", [("tank", "танки"), ("nick", "ники"), ("none", "нет")], "tank")
        + '</span>')
    return (
        '<section class="card replay" id="replay">'
        '<div class="rp-head"><h2>2D-повтор</h2><span class="note">Противники видны, пока их '
        'засвечивают; потом — пунктир там, где их видели в последний раз.</span></div>'
        '<div class="rp-grid"><div class="rp-main">'
        '<div class="rp-stage" id="rp-stage" tabindex="0">'
        f'<canvas id="rp-canvas" aria-label="2D-повтор боя на миникарте: '
        f'{esc(battle["map"]["name"])}"></canvas>{_replay_hud(battle, data["obj"])}'
        '<div class="rp-recent" id="rp-recent"></div></div>'
        f'<div class="rp-timeline"><div class="rp-ticks">{_replay_ticks(battle, end)}</div>'
        f'{_advantage(battle, end)}'
        f'<input type="range" id="rp-scrub" min="0" max="{end:.1f}" step="0.1" value="0" '
        'aria-label="Время боя"></div>'
        f'<div class="rp-controls">{controls}</div></div>'
        f'<aside class="rp-side">{_focus_panel(battle)}<h3>Уничтожения</h3>'
        f'{_feed_list(battle, linked=True)}{_legend(data["obj"])}'
        '<p class="note">Пробел — пауза, ← → — на 5 секунд, Esc — снять выделение.</p>'
        '</aside></div>'
        f'<script type="application/json" id="replay-data">{embed_json(data)}'
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
[hidden]{display:none!important}
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
.rp-hud{position:absolute;top:8px;left:8px;display:flex;flex-direction:column;gap:3px;
align-items:flex-start;pointer-events:none}
.rp-hud div{display:flex;align-items:baseline;gap:4px;font:700 13px var(--mono);
background:rgba(11,14,17,.8);border:1px solid var(--line);border-radius:6px;padding:2px 8px;
white-space:nowrap}
.rp-hud .lbl{font:600 10px var(--body);color:var(--muted);text-transform:uppercase;
letter-spacing:.5px;margin-right:3px}
.rp-hud b.a{color:var(--ally)}.rp-hud b.e{color:var(--enemy)}
.rp-timeline{margin-top:10px}
.rp-ticks{position:relative;height:8px;margin:0 7px}
.rp-ticks i{position:absolute;top:0;width:3px;height:8px;margin-left:-1px;border-radius:1px;
background:var(--muted)}
.rp-ticks i.a{background:var(--ally)}.rp-ticks i.e{background:var(--enemy)}
.rp-ticks i.m{background:var(--me)}
.rp-adv{display:block;width:calc(100% - 14px);height:16px;margin:3px 7px 0}
.rp-adv line{stroke:rgba(255,255,255,.2);stroke-width:1}
.rp-adv rect.h{fill:transparent}
.rp-adv rect.a{fill:var(--ally);opacity:.75}.rp-adv rect.e{fill:var(--enemy);opacity:.75}
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
.rp-legend svg{width:13px;height:13px;flex:none;fill:#C9CED3;stroke:#0B0E11;stroke-width:1}
.rp-legend svg .cut,.rp-legend svg .pole{fill:none;stroke-width:1.5}
.rp-legend svg .pole{stroke:#C9CED3}
.rp-legend .lp{display:inline-block;width:14px;height:14px;border:1.5px solid #C9CED3;
font:700 9px/11px var(--body);text-align:center;color:#C9CED3}
.rp-focus{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
padding:10px 12px;margin-bottom:14px}
.rp-focus .note{margin:0 0 8px}
.rp-focus .rbtn{font-size:12px}
.rc-head{display:flex;align-items:baseline;gap:6px;font-size:15px;font-weight:700}
.rc-close{margin-left:auto;font:inherit;font-size:13px;color:var(--muted);background:none;
border:0;cursor:pointer;padding:0 2px}
.rc-close:hover{color:var(--text)}
.rc-tank{color:var(--muted);font-size:12px;margin-top:2px}
.rc-stats{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:8px;color:var(--muted);font-size:12px}
.rc-stats b{font:700 15px var(--mono);color:var(--text)}
.rc-fate{margin-top:6px;font-size:13px}
.rc-fate a.t{color:inherit}
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
# playable border dimmed, the capture zones with their owners and progress,
# trails, wrecks, "last seen" outlines, live tanks as the game's class marks
# with a wedge for the hull heading, killer -> victim arrows, labels. A click
# on a tank picks it: its whole route, the rest dimmed, its results beside
# the map. Strings from the replay reach the page only through fillText and
# textContent.
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
  var aliveA = document.getElementById('rp-na'), aliveE = document.getElementById('rp-ne');
  var scoreEl = document.getElementById('rp-score'), capEl = document.getElementById('rp-cap');
  var hintEl = document.getElementById('rp-hint'), cardEl = document.getElementById('rp-card');
  var bestBtn = document.getElementById('rp-best');
  var feed = Array.prototype.slice.call(card.querySelectorAll('.feed li[data-t]'));
  var COL = {m: '#E8C25A', a: '#7BBE7B', e: '#D86A5A'};
  var RGB = {m: '232,194,90', a: '123,190,123', e: '216,106,90'};
  var TEXT = {m: '#F6DE94', a: '#BFE6BF', e: '#F4AEA2'};
  var INK = 'rgba(0,0,0,.75)', FONT = 'Inter, "Segoe UI", Roboto, sans-serif';
  var END = R.end, GAP = R.gap, TAU = Math.PI * 2;
  function chosen(group) {  // the defaults are whichever buttons the page marked "on"
    var b = card.querySelector('.seg[data-group="' + group + '"] button.on');
    return b ? b.getAttribute('data-v') : null;
  }
  var speed = +chosen('speed') || 4, trail = +chosen('trail'), labels = chosen('label') || 'tank';
  var cur = 0, playing = false, lastTs = 0, W = 1, H = 1, dpr = 1, recentSig = '', focus = null;

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

  var OBJ = R.obj, pointStates = {}, baseStates = {a: [], e: []};
  if (OBJ) {
    (OBJ.states || []).forEach(function (s) { (pointStates[s[1]] = pointStates[s[1]] || []).push(s); });
    (OBJ.base || []).forEach(function (s) { if (baseStates[s[1]]) baseStates[s[1]].push(s); });
  }

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
  function last(list, t) {  // the last [t, ...] row at or before t, or null
    var lo = 0, hi = list.length - 1, found = null;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (list[mid][0] <= t) { found = list[mid]; lo = mid + 1; } else { hi = mid - 1; }
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

  function flag(x, y) {  // the Encounter base
    var u = dpr;
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.beginPath(); ctx.moveTo(x - 3 * u, y + 7 * u); ctx.lineTo(x - 3 * u, y - 7 * u);
    ctx.strokeStyle = INK; ctx.lineWidth = 3.4 * u; ctx.stroke();
    ctx.strokeStyle = '#FFFFFF'; ctx.lineWidth = 1.6 * u; ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x - 3 * u, y - 7 * u); ctx.lineTo(x + 6 * u, y - 4 * u);
    ctx.lineTo(x - 3 * u, y - u); ctx.closePath();
    ctx.fillStyle = '#FFFFFF'; ctx.fill(); ctx.strokeStyle = INK; ctx.lineWidth = 1.2 * u; ctx.stroke();
  }
  function badge(x, y, text, owner) {  // a Supremacy point: its letter, filled with the owner's colour
    var b = 7.5 * dpr;
    ctx.fillStyle = owner ? COL[owner] : 'rgba(14,17,20,.85)';
    ctx.fillRect(x - b, y - b, 2 * b, 2 * b);
    ctx.strokeStyle = 'rgba(255,255,255,.9)'; ctx.lineWidth = 1.4 * dpr;
    ctx.strokeRect(x - b, y - b, 2 * b, 2 * b);
    ctx.fillStyle = owner ? '#0B0E11' : '#FFFFFF';
    ctx.font = '700 ' + Math.round(11 * dpr) + 'px ' + FONT;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(text, x, y + 0.5 * dpr);
  }
  function drawZones(t) {
    if (!OBJ) return;
    var scale = W / (view[2] - view[0]);
    OBJ.zones.forEach(function (zone) {
      var x = px(zone.x), y = py(zone.z), r = zone.r * scale, owner = '', caps = [];
      if (OBJ.kind === 'points') {
        var s = last(pointStates[zone.id] || [], t);
        if (s) { owner = s[2]; if (s[3] && s[4] > 0) caps.push([s[3], s[4]]); }
      } else {
        ['a', 'e'].forEach(function (side) {
          var s = last(baseStates[side], t);
          if (s && s[2] > 0) caps.push([side, s[2]]);
        });
      }
      ctx.save();
      ctx.beginPath(); ctx.arc(x, y, r, 0, TAU);
      ctx.fillStyle = owner ? 'rgba(' + RGB[owner] + ',.22)' : 'rgba(255,255,255,.1)';
      ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,.45)'; ctx.lineWidth = 3.5 * dpr; ctx.stroke();  // shows on pale maps
      ctx.lineWidth = 1.6 * dpr;
      ctx.strokeStyle = owner ? COL[owner] : 'rgba(255,255,255,.85)';
      if (!owner) ctx.setLineDash([4 * dpr, 3 * dpr]);
      ctx.stroke();
      ctx.setLineDash([]);
      caps.forEach(function (c, i) {  // capture progress, clockwise from the top
        ctx.beginPath();
        ctx.arc(x, y, r - i * 5 * dpr, -Math.PI / 2, -Math.PI / 2 + TAU * Math.min(c[1], 100) / 100);
        ctx.strokeStyle = COL[c[0]]; ctx.lineWidth = 4 * dpr; ctx.stroke();
      });
      if (OBJ.kind === 'points') badge(x, y, zone.l, owner); else flag(x, y);
      ctx.restore();
    });
  }

  function path(v, from, to) {  // a stretch of track, broken where the vehicle was unseen
    var T = v.T, pen = false;
    ctx.beginPath();
    for (var k = from; k <= to; k++) {
      if (k > from && T[k] - T[k - 1] > GAP) pen = false;
      if (pen) ctx.lineTo(px(v.X[k]), py(v.Z[k]));
      else { ctx.moveTo(px(v.X[k]), py(v.Z[k])); pen = true; }
    }
    return pen;
  }
  function drawTrail(v, s, t) {
    var T = v.T, picked = v === focus, span = picked ? -1 : trail;
    if (!span) return;
    var dead = s && s.k === 'dead', stop = dead ? at(T, v.d + 0.25) : at(T, t);
    var from = span > 0 ? Math.max(0, at(T, t - span)) : 0;
    ctx.save();
    ctx.strokeStyle = COL[v.s]; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    if (picked && !dead && stop + 1 < T.length) {  // where it goes next: faint dashes
      path(v, Math.max(stop, 0), v.d !== null ? Math.max(0, at(T, v.d + 0.25)) : T.length - 1);
      ctx.globalAlpha = .5; ctx.lineWidth = 1.6 * dpr; ctx.setLineDash([4 * dpr, 4 * dpr]);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (stop >= 0 && (stop > from || (s && s.k === 'live'))) {
      var pen = path(v, from, stop);
      if (pen && s && s.k === 'live') ctx.lineTo(px(s.x), py(s.z));
      ctx.globalAlpha = picked ? 1 : (v.s === 'm' ? .9 : .55) * (focus ? .3 : 1);
      ctx.lineWidth = (picked ? 3 : v.s === 'm' ? 2.2 : 1.5) * dpr;
      ctx.stroke();
    }
    ctx.restore();
  }

  function shape(c, r) {  // the game's minimap class marks, centred on 0,0
    ctx.beginPath();
    if (c === 'TD') {
      ctx.moveTo(-1.1 * r, -.8 * r); ctx.lineTo(1.1 * r, -.8 * r); ctx.lineTo(0, 1.1 * r); ctx.closePath();
    } else if (c === 'LT' || c === 'MT' || c === 'HT') {
      var d = 1.2 * r;
      ctx.moveTo(0, -d); ctx.lineTo(d, 0); ctx.lineTo(0, d); ctx.lineTo(-d, 0); ctx.closePath();
    } else if (c === 'SPG') {
      ctx.rect(-.9 * r, -.9 * r, 1.8 * r, 1.8 * r);
    } else {
      ctx.arc(0, 0, r, 0, TAU);
    }
  }
  function cuts(c, r) {  // MT: one cut across the diamond, HT: two -- along x + y = k
    var n = c === 'MT' ? 1 : c === 'HT' ? 2 : 0, d = 1.2 * r;
    if (!n) return;
    ctx.save();
    shape(c, r); ctx.clip();
    ctx.beginPath();
    for (var i = 0; i < n; i++) {
      var k = n === 1 ? 0 : (i ? d : -d) / 3;
      ctx.moveTo(2 * d, k - 2 * d); ctx.lineTo(-2 * d, k + 2 * d);
    }
    ctx.strokeStyle = INK; ctx.lineWidth = 1.3 * dpr; ctx.stroke();
    ctx.restore();
  }
  function wedge(yaw, r, c) {  // hull heading: 0 = north, clockwise like the canvas
    var o = 1.25 * r;
    ctx.save();
    ctx.rotate(yaw * Math.PI / 180);
    ctx.beginPath();
    ctx.moveTo(0, -o - 5 * dpr); ctx.lineTo(2.8 * dpr, -o - dpr); ctx.lineTo(-2.8 * dpr, -o - dpr);
    ctx.closePath();
    ctx.fillStyle = c; ctx.fill(); ctx.strokeStyle = INK; ctx.lineWidth = dpr; ctx.stroke();
    ctx.restore();
  }
  function ring(radius, color, width) {
    ctx.beginPath(); ctx.arc(0, 0, radius, 0, TAU);
    ctx.strokeStyle = color; ctx.lineWidth = width * dpr; ctx.stroke();
  }
  function cross(r) {
    ctx.beginPath();
    ctx.moveTo(-r, -r); ctx.lineTo(r, r); ctx.moveTo(r, -r); ctx.lineTo(-r, r);
    ctx.stroke();
  }
  function drawTank(v, s) {
    var r = (v.s === 'm' ? 6 : 5) * dpr, c = COL[v.s];
    ctx.save();
    ctx.translate(px(s.x), py(s.z));
    ctx.globalAlpha = focus && v !== focus ? .3 : 1;
    if (s.k === 'dead') {
      ctx.lineCap = 'round';
      ctx.strokeStyle = 'rgba(0,0,0,.65)'; ctx.lineWidth = 4.5 * dpr; cross(r * .8);
      ctx.strokeStyle = c; ctx.lineWidth = 2.2 * dpr; cross(r * .8);
    } else if (s.k === 'ghost') {
      ctx.globalAlpha *= Math.max(.3, .85 - s.age / 60);
      ctx.setLineDash([2.5 * dpr, 2 * dpr]);
      shape(v.c, r); ctx.strokeStyle = c; ctx.lineWidth = 1.6 * dpr; ctx.stroke();
      ctx.setLineDash([]);
    } else {
      wedge(s.y, r, c);
      shape(v.c, r); ctx.fillStyle = c; ctx.fill();
      cuts(v.c, r);
      shape(v.c, r); ctx.strokeStyle = INK; ctx.lineWidth = 1.3 * dpr; ctx.stroke();
      if (v.s === 'm') ring(1.25 * r + 3.5 * dpr, 'rgba(255,255,255,.9)', 1.5);
    }
    if (v === focus) {
      ctx.globalAlpha = 1;
      ring(1.25 * r + 8 * dpr, 'rgba(0,0,0,.6)', 4);
      ring(1.25 * r + 8 * dpr, '#FFFFFF', 2);
    }
    ctx.restore();
  }
  function drawLabel(v, s) {
    var picked = v === focus;
    if (labels === 'none' && !picked) return;
    var text = picked || labels === 'nick' ? v.n : v.k;
    if (s.k === 'ghost') text += ' · ' + Math.round(s.age) + ' с';
    var x = px(s.x), y = py(s.z) + (picked ? 16 : s.k === 'dead' ? 7 : 10) * dpr;
    ctx.save();
    ctx.font = '600 ' + Math.round(11 * dpr) + 'px ' + FONT;
    ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.lineJoin = 'round';
    ctx.globalAlpha = (s.k === 'live' || picked ? 1 : .65) * (focus && !picked ? .3 : 1);
    ctx.lineWidth = 3 * dpr; ctx.strokeStyle = 'rgba(0,0,0,.85)';
    ctx.strokeText(text, x, y);
    ctx.fillStyle = s.k === 'dead' && !picked ? '#A2AAB3' : TEXT[v.s];
    ctx.fillText(text, x, y);
    ctx.restore();
  }

  function seen(v, t) {  // where a vehicle could be seen at t, or null
    if (!v || !v.T) return null;
    var s = state(v, t);
    if (s && (s.k === 'live' || (s.k === 'dead' && t - v.d < 1.5))) return s;
    return null;
  }
  function arrow(x0, y0, x1, y1, color) {  // from the killer to the victim, clear of both marks
    var dx = x1 - x0, dy = y1 - y0, len = Math.hypot(dx, dy);
    if (len < 24 * dpr) return;
    var ux = dx / len, uy = dy / len, hs = 7 * dpr;
    var sx = x0 + ux * 9 * dpr, sy = y0 + uy * 9 * dpr, ex = x1 - ux * 11 * dpr, ey = y1 - uy * 11 * dpr;
    ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex - ux * hs * .8, ey - uy * hs * .8);
    ctx.strokeStyle = INK; ctx.lineWidth = 4 * dpr; ctx.stroke();
    ctx.strokeStyle = color; ctx.lineWidth = 2 * dpr; ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(ex, ey);
    ctx.lineTo(ex - ux * hs - uy * hs * .6, ey - uy * hs + ux * hs * .6);
    ctx.lineTo(ex - ux * hs + uy * hs * .6, ey - uy * hs - ux * hs * .6);
    ctx.closePath();
    ctx.fillStyle = color; ctx.fill(); ctx.strokeStyle = INK; ctx.lineWidth = dpr; ctx.stroke();
  }
  function caption(text, x, y) {
    ctx.font = '600 ' + Math.round(11 * dpr) + 'px ' + FONT;
    ctx.textAlign = 'center'; ctx.textBaseline = 'bottom'; ctx.lineJoin = 'round';
    ctx.lineWidth = 3 * dpr; ctx.strokeStyle = 'rgba(0,0,0,.85)'; ctx.strokeText(text, x, y);
    ctx.fillStyle = '#E4E7EA'; ctx.fillText(text, x, y);
  }
  function drawKills(t) {  // the first two seconds after each kill
    R.kills.forEach(function (k) {
      var age = t - k.t, victim = byId[k.v];
      if (age < 0 || age > 2 || !victim || !victim.T) return;
      var vs = state(victim, k.t + 0.05);
      if (!vs) return;
      var x = px(vs.x), y = py(vs.z), killer = k.k !== k.v ? byId[k.k] : null, ks = seen(killer, k.t);
      ctx.save();
      ctx.globalAlpha = 1 - age / 2;
      if (ks) arrow(px(ks.x), py(ks.z), x, y, COL[killer.s]);
      else if (killer && killer.s === 'e') caption('стрелок не в засвете', x, y - 12 * dpr);
      ctx.globalAlpha = Math.max(0, 1 - age / 1.5);
      ctx.strokeStyle = '#FFFFFF'; ctx.lineWidth = 2 * dpr;
      ctx.beginPath(); ctx.arc(x, y, (8 + age * 22) * dpr, 0, TAU); ctx.stroke();
      ctx.restore();
    });
  }

  function render() {
    ctx.clearRect(0, 0, W, H);
    drawMap();
    drawZones(cur);
    var states = V.map(function (v) { return state(v, cur); });
    V.forEach(function (v, i) { drawTrail(v, states[i], cur); });
    ['dead', 'ghost', 'live'].forEach(function (kind) {
      V.forEach(function (v, i) {
        if (v !== focus && states[i] && states[i].k === kind) drawTank(v, states[i]);
      });
    });
    var f = focus ? V.indexOf(focus) : -1;
    if (f >= 0 && states[f]) drawTank(focus, states[f]);  // the picked tank on top
    V.forEach(function (v, i) { if (states[i]) drawLabel(v, states[i]); });
    drawKills(cur);  // over the labels: it only lasts two seconds
  }

  function fmt(t) {
    t = Math.max(0, Math.floor(t));
    return Math.floor(t / 60) + ':' + ('0' + t % 60).slice(-2);
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function who(v) { return el('span', v ? v.s : 'muted', v ? (labels === 'nick' ? v.n : v.k) : '—'); }
  function showRecent(t) {
    var list = R.kills.filter(function (k) { return k.t <= t && t - k.t <= 12; }).slice(-5).reverse();
    var sig = labels + list.map(function (k) { return k.t + ':' + k.v; }).join('|');
    if (sig === recentSig) return;
    recentSig = sig;
    recentEl.textContent = '';
    list.forEach(function (k) {
      var row = el('div');
      row.appendChild(who(byId[k.k]));
      row.appendChild(document.createTextNode(' ✕ '));
      row.appendChild(who(byId[k.v]));
      recentEl.appendChild(row);
    });
  }
  function showBoard(t) {
    var na = 0, ne = 0;
    R.vehicles.forEach(function (v) {
      if (v.d === null || t < v.d) { if (v.s === 'e') ne++; else na++; }
    });
    aliveA.textContent = na; aliveE.textContent = ne;
    if (scoreEl) {
      var s = last(OBJ.score, t), b = scoreEl.getElementsByTagName('b');
      b[0].textContent = s && s[1] !== null ? s[1] : '—';
      b[1].textContent = s && s[2] !== null ? s[2] : '—';
    }
    if (capEl) {
      var caps = [];
      ['a', 'e'].forEach(function (side) {
        var c = last(baseStates[side], t);
        if (c && c[2] > 0) caps.push([side, c[2]]);
      });
      capEl.hidden = !caps.length;
      if (caps.length) {  // both sides at once is rare; the map shows both arcs
        caps.sort(function (a, b) { return b[1] - a[1]; });
        var label = capEl.getElementsByTagName('b')[0];
        label.className = caps[0][0];
        label.textContent = Math.min(caps[0][1], 100) + '%';
      }
    }
  }
  function num(n) { return n === null || n === undefined ? '—' : Math.round(n).toLocaleString('ru-RU'); }
  function showCard() {
    hintEl.hidden = !!focus;
    cardEl.hidden = !focus;
    cardEl.textContent = '';
    if (!focus) return;
    var v = focus, head = el('div', 'rc-head');
    head.appendChild(el('span', v.s, v.n));
    if (v.cl) head.appendChild(el('span', 'clan', '[' + v.cl + ']'));
    var close = el('button', 'rc-close', '✕');
    close.type = 'button'; close.title = 'Снять выделение (Esc)';
    close.addEventListener('click', function () { pick(null); });
    head.appendChild(close);
    cardEl.appendChild(head);
    cardEl.appendChild(el('div', 'rc-tank', v.tn + (v.tk ? ' · ' + v.tk : '')));
    if (v.r) {
      var stats = el('div', 'rc-stats');
      [[v.r.dmg, 'урон'], [v.r.fr, 'фраги'], [v.r.as, 'помощь'], [v.r.bl, 'блок']].forEach(function (p) {
        var item = el('span');
        item.appendChild(el('b', null, num(p[0])));
        item.appendChild(document.createTextNode(' ' + p[1]));
        stats.appendChild(item);
      });
      cardEl.appendChild(stats);
    }
    var fate = el('div', 'rc-fate');
    if (v.d !== null) {
      fate.appendChild(document.createTextNode('Уничтожен на '));
      var link = el('a', 't', fmt(v.d));
      link.href = '#t=' + Math.max(0, Math.floor(v.d - 5));
      fate.appendChild(link);
      var killer = byId[v.kid];
      if (killer && killer !== v) {
        fate.appendChild(document.createTextNode(' — '));
        fate.appendChild(el('span', killer.s, killer.n));
      }
    } else if (v.r && v.r.hp > 0) {
      fate.textContent = 'Выжил: ' + num(v.r.hp) + (v.r.mhp ? ' из ' + num(v.r.mhp) : '') + ' HP';
    } else if (v.r) {
      fate.textContent = 'Уничтожен уже после конца записи';
    } else {
      fate.textContent = 'В строю на конец записи';
    }
    cardEl.appendChild(fate);
    if (!v.T) cardEl.appendChild(el('p', 'note', 'Его ни разу не засветили — пути на карте нет.'));
  }
  function draw() {
    render();
    var shown = Math.round(cur * 10) / 10;  // the slider's own rounding
    scrub.value = shown.toFixed(1);
    timeEl.textContent = fmt(shown) + ' / ' + fmt(END);
    feed.forEach(function (li) {
      var t = +li.getAttribute('data-t');
      li.classList.toggle('past', t <= cur);
      li.classList.toggle('now', t <= cur && cur - t <= 4);
    });
    showRecent(cur);
    showBoard(cur);
  }

  function pick(v) {
    focus = v || null;
    showCard();
    draw();
  }
  function hit(e) {  // the tank under the pointer, live ones first
    var box = cv.getBoundingClientRect();
    if (!box.width) return null;
    var x = (e.clientX - box.left) * W / box.width, y = (e.clientY - box.top) * H / box.height;
    var best = null, bestD = 16 * dpr;
    V.forEach(function (v) {
      var s = state(v, cur);
      if (!s) return;
      var d = Math.hypot(px(s.x) - x, py(s.z) - y) + (s.k === 'live' ? 0 : 6 * dpr);
      if (d < bestD) { best = v; bestD = d; }
    });
    return best;
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
  stage.addEventListener('click', function (e) {  // a tank picks it, anywhere else plays / pauses
    var v = hit(e);
    if (v) pick(v === focus ? null : v); else play(!playing);
  });
  stage.addEventListener('mousemove', function (e) {
    var v = hit(e);
    stage.title = v ? v.n + ' · ' + v.k : '';
  });
  if (bestBtn) bestBtn.addEventListener('click', function () { pick(byId[+bestBtn.getAttribute('data-id')]); });
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
    else if (e.key === 'Escape' && focus) { pick(null); }
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
