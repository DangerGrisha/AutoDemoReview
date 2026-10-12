"""HTML report for one Blitz battle -- self-contained (inline CSS and JS).

Every string that comes from a replay (nicknames, clan tags) is controlled by
whoever made the file and goes through `esc`; numbers are formatted, never
interpolated raw. The 2D replay's data goes into a JSON block through
`embed_json`, and its script only ever puts those strings on the page as text
(canvas text, textContent). The minimap is embedded as a data: URI, so a saved
page still renders. `site_nav` is trusted HTML built by the web layer.
"""

import base64
from datetime import datetime, timezone

from reviewkit import theme
from reviewkit.html import embed_json, esc  # noqa: F401 -- re-exported for older callers

from . import analysis, coachrules, gamedata

OUTCOME = {
    "win": ("Победа", "win"),
    "loss": ("Поражение", "loss"),
    "draw": ("Ничья", "draw"),
    "unknown": ("Итог неизвестен", "unknown"),
}


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


SEVERITY_LABEL = {"high": "критично", "medium": "заметно", "low": "мелочь"}


def _coach(battle):
    """"Разбор боя": the free coach's report (coachrules.py) about the recorder,
    every cited moment a link into the 2D replay. A coach bug must not take the
    whole page down, so it shows as a short note instead."""
    try:
        _facts, report = coachrules.analyse_and_build(battle)
    except Exception as exc:  # noqa: BLE001
        return ('<section class="card coach"><h2>Разбор боя</h2><p class="note">Разбор не '
                f'удалось собрать ({esc(type(exc).__name__)}).</p></section>')
    if report is None:
        return ""
    linked = _replay_geo(battle) is not None

    def moment(t):
        if linked:
            return f'<a class="t" href="{_seek(t)}">{clock(t)}</a>'
        return clock(t)

    parts = [f'<p class="c-head">{esc(report["headline"])}</p>']
    if report["strengths"]:
        items = "".join(f'<li><b>{esc(s["point"])}</b> — {esc(s["evidence"])}</li>'
                        for s in report["strengths"])
        parts.append(f'<h3>Что получилось</h3><ul class="c-good">{items}</ul>')
    if report["problems"]:
        blocks = []
        for p in report["problems"]:
            moments = ""
            if p["moments"]:
                moments = ('<p class="c-moments">Моменты: '
                           + ", ".join(moment(t) for t in p["moments"]) + "</p>")
            blocks.append(
                f'<div class="c-prob {esc(p["severity"])}"><div class="c-title">{esc(p["title"])}'
                f'<span class="sev">{SEVERITY_LABEL.get(p["severity"], "")}</span></div>'
                f'<p><b>Почему:</b> {esc(p["cause"])}</p><p><b>Данные:</b> {esc(p["evidence"])}</p>'
                f'{moments}<p><b>Что делать:</b> {esc(p["fix"])}</p></div>')
        parts.append(f'<h3>Что стоит исправить</h3>{"".join(blocks)}')
    drill = report["drill"]
    parts.append(f'<h3>Что тренировать</h3><div class="c-drill"><b>{esc(drill["name"])}</b> — '
                 f'{esc(drill["how"])}<div class="muted">Цель: {esc(drill["target"])}</div></div>')
    return (f'<section class="card coach" id="coach"><h2>Разбор боя</h2>{"".join(parts)}'
            '<p class="note">Бесплатный разбор по правилам: смотрит только этот бой и сравнивает '
            'вас с лобби. Повторяющиеся ошибки по многим боям — на странице карьеры.</p></section>')


# What one hit did, in words: hits.kind (events.HIT_*) when there was no
# damage. Kind 0 is a ricochet the shell flew on from; kind 4 without damage
# hit a module.
_HIT_WORDS = {0: "рикошет", 1: "рикошет", 2: "не пробил", 3: "пробил", 4: "крит"}
_REASON_WORDS = {1: "пожар", 2: "таран", 3: "падение"}  # events.REASON_* other than a shell


def _my_shots(battle, me_id):
    """The recorder's shots in order: [{t, target, word, damage}]. A ricochet
    the shell flew on from counts with where that flight ended."""
    shots = (battle.get("events") or {}).get("shots") or []
    out, current = [], None
    for t, shooter, _x0, _z0, _x1, _z1, victim, kind, damage, bounce in shots:
        if shooter != me_id:
            continue
        if not bounce or current is None:
            current = {"t": t, "hits": []}
            out.append(current)
        if victim:
            current["hits"].append((victim, kind, damage))
    rows = []
    for shot in out:
        hits = shot["hits"]
        damage = sum(h[2] for h in hits)
        if damage:
            victim = next(h[0] for h in hits if h[2])
            word = _HIT_WORDS[3] if any(h[1] == 3 for h in hits if h[2]) else "попал"
        elif hits:
            victim, kind = hits[-1][0], hits[-1][1]
            word = _HIT_WORDS.get(kind, "без урона")
        else:
            victim, word = None, "промах"
        rows.append({"t": shot["t"], "target": victim, "word": word, "damage": damage})
    return rows


def _other_damage(battle, keep):
    """Damage that came without a shell (fire, ramming, falls) for the
    drops `keep(attacker, victim)` accepts: [{t, attacker, victim, word,
    damage}], one row per fire or shoving match -- ticks from the same source
    under two seconds apart are added up."""
    rows = []
    for t, attacker, victim, amount, reason in (battle.get("events") or {}).get("damage") or ():
        if not reason or not keep(attacker, victim):
            continue
        last = rows[-1] if rows else None
        if (last and last["attacker"] == attacker and last["victim"] == victim
                and last["reason"] == reason and t - last["end"] < 2):
            last["damage"] += amount
            last["end"] = t
        else:
            rows.append({"t": t, "end": t, "attacker": attacker, "victim": victim,
                         "reason": reason, "word": _REASON_WORDS.get(reason, "урон"),
                         "damage": amount})
    return rows


def _hits_on_me(battle, me_id):
    """What hit or hurt the recorder: [{t, source, word, damage, shell}] --
    shells (with or without damage), fire, ramming and falls."""
    ev = battle.get("events") or {}
    rows = [{"t": t, "source": attacker, "word": _HIT_WORDS.get(kind, "попал") if not damage
             else "пробил" if kind == 3 else "попал", "damage": damage, "shell": True}
            for t, attacker, victim, kind, damage in ev.get("hits") or () if victim == me_id]
    rows += [dict(r, source=r["attacker"] if r["attacker"] != me_id else None, shell=False)
             for r in _other_damage(battle, lambda attacker, victim: victim == me_id)]
    rows.sort(key=lambda r: r["t"])
    return rows


def _who(vehicle):
    if vehicle is None:
        return '<span class="muted">—</span>'
    return (f'<span class="{_side_class(vehicle)}">{esc(vehicle["nickname"])}</span> '
            f'<span class="tank">{esc(vehicle["tank"]["short"])}</span>')


def _fight_chart(battle, me, shots, end):
    """The recorder's battle over time: damage dealt so far (gold) and hit
    points left (green), a tick under it for every shot -- gold hit for
    damage, grey hit for none, dark missed -- and marks for kills and death."""
    ev = battle["events"]
    me_id = me["entity_id"]
    dealt, total = [(0.0, 0)], 0
    for t, attacker, victim, amount, _reason in ev.get("damage") or ():
        if attacker == me_id and victim != me_id:
            total += amount
            dealt.append((t, total))
    hp_entry = next((h for h in ev.get("hp") or () if h["id"] == me_id), None)
    health = list(zip(hp_entry["p"][0::2], hp_entry["p"][1::2])) if hp_entry else []
    top = max([1, total] + ([hp_entry["max"]] if hp_entry else [])) * 1.08

    def y(value):
        return 86 - 78 * value / top

    def steps(points, stop):
        if not points:
            return ""
        d = [f"M{max(points[0][0], 0):.1f} {y(points[0][1]):.2f}"]
        for t, value in points[1:]:
            d.append(f"H{max(t, 0):.1f}V{y(value):.2f}")
        d.append(f"H{stop:.1f}")
        return "".join(d)

    death = me.get("death_time")
    parts = [f'<path class="g" d="M0 {y(0):.2f}H{end:.1f}"/>',
             f'<path class="hpl" d="{steps(health, death if death is not None else end)}"/>',
             f'<path class="dl" d="{steps(dealt, end)}"/>']
    for shot in shots:
        cls = "sd" if shot["damage"] else "sn" if shot["target"] else "sm"
        parts.append(f'<a href="{_seek(shot["t"] - 2)}"><g><title>{clock(shot["t"])}: '
                     f'{esc(shot["word"])}{" −" + num(shot["damage"]) if shot["damage"] else ""}'
                     f'</title><rect class="hit" x="{shot["t"] - 2:.1f}" y="88" width="4" height="12"/>'
                     f'<path class="{cls}" d="M{shot["t"]:.1f} 90V100"/></g></a>')
    for kill in battle["kills"]:
        if kill["t"] is None:
            continue
        mine = kill["killer"] == me_id and kill["victim"] != me_id
        if mine or kill["victim"] == me_id:
            cls, what = ("kf", "уничтожен противник") if mine else ("kd", "вы уничтожены")
            parts.append(f'<a href="{_seek(kill["t"] - 3)}"><g><title>{clock(kill["t"])}: {what}'
                         f'</title><rect class="hit" x="{kill["t"] - 2:.1f}" y="0" width="4" height="88"/>'
                         f'<path class="{cls}" d="M{kill["t"]:.1f} 2V86"/></g></a>')
    marks = []
    for minute in range(0, int(end) + 1, 60):
        marks.append(f'<span style="left:{100 * minute / end:.2f}%">{clock(minute)}</span>')
    return (f'<div class="fchart"><svg viewBox="0 0 {end:.1f} 100" preserveAspectRatio="none" '
            'role="img" aria-label="Урон и прочность по ходу боя">'
            f'{"".join(parts)}</svg><div class="faxis">{"".join(marks)}</div></div>')


def _fight(battle):
    """"Ход боя": the recorder's damage and hit points over time, every shot
    they fired and everything that hit them, each linked to the 2D replay.
    Battles stored before the events were decoded ask for a fresh upload."""
    me = analysis.author(battle)
    if me is None or not _replay_geo(battle):
        return ""
    if not battle.get("events"):
        if (battle.get("format") or 1) < 4:
            return ('<section class="card"><h2>Ход боя</h2><p class="note">Выстрелы, попадания и '
                    'урон по времени появились позже, чем этот бой сохранён: загрузите реплей '
                    'заново, чтобы их увидеть.</p></section>')
        return ""
    names = {v["entity_id"]: v for v in battle["vehicles"]}
    me_id = me["entity_id"]
    shots = _my_shots(battle, me_id)
    against = _hits_on_me(battle, me_id)
    extra = {}  # my damage that was not a shell: fire my shells started, ramming
    for row in _other_damage(battle, lambda attacker, victim: attacker == me_id != victim):
        extra[row["word"]] = extra.get(row["word"], 0) + row["damage"]
    end = battle["replay"]["end"]

    def table(title, rows, who_key, empty, foot=""):
        if not rows:
            return f'<div><h3>{title}</h3><p class="note">{empty}</p>{foot}</div>'
        body = "".join(
            f'<tr><td><a class="t" href="{_seek(r["t"] - 2)}">{clock(r["t"])}</a></td>'
            f'<td>{_who(names.get(r[who_key])) if r[who_key] else ""}</td>'
            f'<td class="w{" hurt" if r["damage"] else ""}">{esc(r["word"])}</td>'
            f'<td class="n">{"−" + num(r["damage"]) if r["damage"] else ""}</td></tr>'
            for r in rows)
        return (f'<div><h3>{title}</h3><table class="flist"><tbody>{body}</tbody></table>'
                f'{foot}</div>')

    landed = sum(1 for s in shots if s["target"])
    hits_on_me = sum(1 for r in against if r["shell"])
    foot = ""
    if extra:
        foot = ('<p class="note">И не снарядом: '
                + ", ".join(f"{esc(word)} −{num(amount)}" for word, amount in extra.items()) + ".</p>")
    legend = ('<div class="flegend"><span><i class="dl"></i>ваш урон</span>'
              '<span><i class="hpl"></i>ваша прочность</span>'
              '<span><i class="sd"></i>выстрел с уроном</span><span><i class="sn"></i>попал без урона</span>'
              '<span><i class="sm"></i>промах</span><span><i class="kf"></i>ваш фраг</span>'
              '<span><i class="kd"></i>гибель</span></div>')
    return (f'<section class="card fight"><h2>Ход боя</h2>{_fight_chart(battle, me, shots, end)}'
            f'{legend}<div class="fgrid">'
            + table(f"Ваши выстрелы: {len(shots)}, в цель {landed}", shots, "target",
                    "В записи нет ваших выстрелов.", foot)
            + table(f"Попадания по вам: {hits_on_me}", against, "source", "По вам не попали.")
            + '</div><p class="note">Всё, что видно в записи; клик по времени — момент в 2D-повторе.'
            '</p></section>')


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


def _replay_events(battle):
    """Health, shots, hits and damage for the replay script (see battle._events);
    None for a battle stored before format 4."""
    ev = battle.get("events")
    if not ev:
        return None
    return {"hp": ev["hp"], "shots": ev["shots"], "hits": ev["hits"], "damage": ev["damage"]}


def _replay_terrain(battle):
    """The map's heights for the 3D view: {"n", "dz": base64 of the zlib file}
    (see gamedata.terrain; the browser inflates it), or None without terrain."""
    found = gamedata.terrain(battle["map"].get("key"))
    if found is None:
        return None
    return {"n": found["n"], "dz": base64.b64encode(found["data"]).decode("ascii")}


def _replay_data(battle, geo):
    rp = battle["replay"]
    return {
        "end": rp["end"], "gap": rp["gap"], "fmt": rp.get("format", 1),
        "world": geo["world"], "crop": geo["crop"], "border": geo["border"],
        "view": geo["view"],
        "vehicles": [_replay_vehicle(v) for v in battle["vehicles"]],
        "tracks": rp["tracks"],
        "kills": [{"t": k["t"], "v": k["victim"], "k": k["killer"]}
                  for k in battle["kills"] if k["t"] is not None],
        "obj": _replay_objectives(battle),
        "ev": _replay_events(battle),
        "terrain": _replay_terrain(battle),
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


def _legend(obj, fight=False):
    classes = "".join(f'<span>{_mark(_MARKS[code])}{gamedata.class_name(code)}</span>'
                      for code in ("LT", "MT", "HT", "TD"))
    shots = ('<span><i class="lb"></i>куда смотрит орудие</span><span><i class="lt"></i>выстрел</span>'
             '<span><b class="ld">−350</b>урон</span><span><i class="lh"></i>прочность</span>'
             if fight else "")
    zones = ""
    if obj and obj["kind"] == "points":
        zones = '<span><b class="lp">A</b>точки захвата</span>'
    elif obj:
        zones = f'<span>{_mark(_FLAG)}база</span>'
    return ('<div class="rp-legend"><span><i class="lm"></i>вы</span>'
            '<span><i class="la"></i>союзники</span><span><i class="le"></i>противники</span>'
            f'{classes}{shots}<span><i class="ls"></i>засвечен</span>'
            '<span><i class="lg"></i>последний раз видели</span>'
            f'<span>✕ уничтожен</span><span>→ кто кого уничтожил</span>{zones}</div>')


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
        + '</span>'
        + ('<span class="rp-opt">вид' + _seg("view", [("2d", "2D"), ("3d", "3D")], "2d") + '</span>'
           if data["terrain"] else ""))
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
        f'<div class="rp-controls">{controls}</div>'
        '<p class="note rp-3dnote" id="rp-3dnote" hidden></p></div>'
        f'<aside class="rp-side">{_focus_panel(battle)}<h3>Уничтожения</h3>'
        f'{_feed_list(battle, linked=True)}{_legend(data["obj"], data["ev"] is not None)}'
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

_CSS = (":root{" + theme.css_vars() + ";--ally:#7BBE7B;--enemy:#D86A5A;--me:#E8C25A}") + """
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
.rp-stage.is3d{touch-action:none}
.rp-3dnote{margin:6px 0 0}
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
.rp-legend .ls{border:2px solid var(--enemy);width:12px;height:12px}
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
.rc-live{margin-top:8px}
.rc-lbl{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);align-self:center}
.rc-hp{display:block;height:5px;border-radius:3px;background:var(--line);margin:4px 0 0;overflow:hidden}
.rc-hp i{display:block;height:100%}
.rc-live .rc-stats{margin-top:4px}
.rc-note{color:var(--muted);font-size:11px;margin-top:4px}
.rp-legend .lb{width:3px;height:13px;border-radius:2px;background:#F6DE94;box-shadow:0 0 0 1px rgba(0,0,0,.7)}
.rp-legend .lt{width:16px;height:2px;border-radius:1px;background:#BFE6BF;box-shadow:0 0 0 1px rgba(0,0,0,.6)}
.rp-legend .lh{width:16px;height:4px;border-radius:0;background:linear-gradient(90deg,var(--ally) 65%,#000 65%)}
.rp-legend .ld{font:700 11px var(--body);color:#FFD27A}
.coach .c-head{font-size:16px;font-weight:600;margin:0 0 4px}
.coach h3,.fgrid h3{font:700 16px var(--display);letter-spacing:.5px;text-transform:uppercase;
margin:14px 0 6px}
.c-good{margin:0;padding-left:18px}
.c-good li{margin:3px 0}
.c-prob{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--muted);
border-radius:8px;padding:10px 12px;margin:8px 0}
.c-prob.high{border-left-color:var(--enemy)}
.c-prob.medium{border-left-color:var(--me)}
.c-title{font-weight:700;font-size:15px}
.c-prob p{margin:5px 0 0;font-size:13px}
.c-prob .sev{font:600 11px var(--mono);text-transform:uppercase;letter-spacing:.4px;
color:var(--muted);margin-left:8px}
.c-prob.high .sev{color:var(--enemy)}
.c-prob.medium .sev{color:var(--me)}
.c-moments a.t{margin-right:2px}
.c-drill{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.c-drill .muted{margin-top:4px;font-size:13px}
.fchart svg{display:block;width:100%;height:150px;background:var(--panel2);border:1px solid var(--line);
border-radius:8px}
.fchart path{fill:none;vector-effect:non-scaling-stroke}
.fchart .g{stroke:rgba(255,255,255,.18);stroke-width:1}
.fchart .dl{stroke:var(--me);stroke-width:2.2}
.fchart .hpl{stroke:var(--ally);stroke-width:2.2}
.fchart .sd{stroke:var(--me);stroke-width:2}
.fchart .sn{stroke:#A2AAB3;stroke-width:2}
.fchart .sm{stroke:#4F5861;stroke-width:2}
.fchart .kf{stroke:var(--me);stroke-width:1.5;stroke-dasharray:4 3}
.fchart .kd{stroke:var(--enemy);stroke-width:2}
.fchart rect.hit{fill:transparent}
.fchart a:hover path{stroke-width:4}
.faxis{position:relative;height:16px;margin:3px 1px 0;font:11px var(--mono);color:var(--muted)}
.faxis span{position:absolute;transform:translateX(-50%)}
.faxis span:first-child{transform:none}
.flegend{display:flex;flex-wrap:wrap;gap:4px 14px;margin:4px 0 0;color:var(--muted);font-size:12px}
.flegend span{display:inline-flex;align-items:center;gap:6px}
.flegend i{display:inline-block;width:14px;height:3px;border-radius:2px}
.flegend i.dl,.flegend i.sd{background:var(--me)}
.flegend i.hpl{background:var(--ally)}
.flegend i.sn{background:#A2AAB3}
.flegend i.sm{background:#4F5861}
.flegend i.sd,.flegend i.sn,.flegend i.sm,.flegend i.kd{width:3px;height:12px}
.flegend i.kd{background:var(--enemy)}
.flegend i.kf{width:0;height:12px;border-left:2px dashed var(--me);border-radius:0}
.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:6px 24px;margin-top:14px}
.fgrid h3{margin:0 0 4px}
.flist td{padding:4px 6px}
.flist td:first-child{width:44px;padding-left:0}
.flist td:nth-child(2){white-space:normal}
.flist .tank{color:var(--muted);font-size:12px}
.flist .w{color:var(--muted)}
.flist .w.hurt{color:var(--text)}
@media(max-width:760px){.fgrid{grid-template-columns:minmax(0,1fr)}}
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
# the map. The 3D switch draws the terrain (heights from the client, the
# minimap on top) and the tanks with WebGL -- no library -- and keeps every
# overlay: they all go through pt(), which projects through the camera.
# Strings from the replay reach the page only through fillText and
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
  var STRIDE = R.fmt >= 2 ? 5 : 4;  // format 1 tracks (older battles) have no turret
  R.vehicles.forEach(function (v) { byId[v.id] = v; });
  R.tracks.forEach(function (tr) {
    var v = byId[tr.id];
    if (!v) return;
    var p = tr.p, n = Math.floor(p.length / STRIDE);
    var T = new Float64Array(n), X = new Float64Array(n), Z = new Float64Array(n),
        Y = new Float64Array(n), G = new Float64Array(n), t = 0, x = 0, z = 0, y = 0, g = 0;
    for (var i = 0; i < n; i++) {
      var k = STRIDE * i;
      if (i === 0) { t = p[0]; x = p[1]; z = p[2]; y = p[3]; g = STRIDE > 4 ? p[4] : 0; }
      else { t += p[k]; x += p[k + 1]; z += p[k + 2]; y += p[k + 3]; if (STRIDE > 4) g += p[k + 4]; }
      T[i] = t / 10; X[i] = x / 10; Z[i] = z / 10; Y[i] = y; G[i] = g;  // yaws unwrapped: lerp-safe
    }
    v.T = T; v.X = X; v.Z = Z; v.Y = Y; v.G = STRIDE > 4 ? G : null;
    v.spotted = [];  // when an enemy came into view: its first sample and each one after a silence
    if (v.s === 'e') for (var j = 0; j < n; j++) if (!j || T[j] - T[j - 1] > GAP) v.spotted.push(T[j]);
    V.push(v);
  });

  var EV = R.ev, SHOTS = EV ? EV.shots : [], HITS = EV ? EV.hits : [], DMG = EV ? EV.damage : [];
  if (EV) EV.hp.forEach(function (h) {  // [t, hp, t, hp, ...] -> two arrays for at()
    var v = byId[h.id];
    if (!v) return;
    var n = h.p.length / 2, T = new Float64Array(n), P = new Float64Array(n);
    for (var i = 0; i < n; i++) { T[i] = h.p[2 * i]; P[i] = h.p[2 * i + 1]; }
    v.hpT = T; v.hpV = P; v.hpMax = h.max;
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
  var MODE = '2d';  // or '3d': the same overlays, projected through the camera
  function pt(x, z, lift) {  // a world point on the screen, `lift` metres above the ground; null behind the camera
    if (MODE === '2d') return {x: px(x), y: py(z)};
    return project(x, ground(x, z) + (lift || 0), z);
  }
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
  function since(list, t) {  // index of the first [t, ...] row at or after t
    var lo = 0, hi = list.length;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (list[mid][0] < t) lo = mid + 1; else hi = mid;
    }
    return lo;
  }
  function hpAt(v, t) {  // health as last seen at t, null before the first sighting
    if (!v.hpT) return null;
    var i = at(v.hpT, t);
    return i < 0 ? null : v.hpV[i];
  }
  function state(v, t) {  // g: where the turret points relative to the hull (null: unknown)
    var T = v.T, n = T.length, G = v.G;
    if (v.d !== null && t >= v.d) {
      var j = at(T, v.d + 0.25);
      return j < 0 ? null : {k: 'dead', x: v.X[j], z: v.Z[j], y: v.Y[j], g: null};
    }
    var i = at(T, t);
    if (i < 0) return null;
    if (i + 1 < n && T[i + 1] - T[i] <= GAP) {
      var f = (t - T[i]) / (T[i + 1] - T[i]);
      return {k: 'live', x: v.X[i] + (v.X[i + 1] - v.X[i]) * f,
              z: v.Z[i] + (v.Z[i + 1] - v.Z[i]) * f, y: v.Y[i] + (v.Y[i + 1] - v.Y[i]) * f,
              g: G ? G[i] + (G[i + 1] - G[i]) * f : null};
    }
    var age = t - T[i];
    if (age <= GAP) return {k: 'live', x: v.X[i], z: v.Z[i], y: v.Y[i], g: G ? G[i] : null};
    return {k: 'ghost', x: v.X[i], z: v.Z[i], y: v.Y[i], g: null, age: age};
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
  function circle(zone, radius, from, to) {  // a path round a zone: flat in 2D, laid on the ground in 3D
    ctx.beginPath();
    if (MODE === '2d') {
      ctx.arc(px(zone.x), py(zone.z), radius * W / (view[2] - view[0]), from - Math.PI / 2, to - Math.PI / 2);
      return;
    }
    var pen = false;
    for (var k = 0; k <= 48; k++) {  // angles clockwise from north, like the 2D arcs
      var a = from + (to - from) * k / 48, q = pt(zone.x + radius * Math.sin(a), zone.z + radius * Math.cos(a), 0.4);
      if (!q) { pen = false; continue; }
      if (pen) ctx.lineTo(q.x, q.y); else { ctx.moveTo(q.x, q.y); pen = true; }
    }
  }
  function drawZones(t) {
    if (!OBJ) return;
    var scale = W / (view[2] - view[0]);
    OBJ.zones.forEach(function (zone) {
      var c = pt(zone.x, zone.z, 0.4), r = zone.r, owner = '', caps = [];
      if (!c) return;
      var x = c.x, y = c.y;
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
      circle(zone, r, 0, TAU);
      ctx.fillStyle = owner ? 'rgba(' + RGB[owner] + ',.22)' : 'rgba(255,255,255,.1)';
      ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,.45)'; ctx.lineWidth = 3.5 * dpr; ctx.stroke();  // shows on pale maps
      ctx.lineWidth = 1.6 * dpr;
      ctx.strokeStyle = owner ? COL[owner] : 'rgba(255,255,255,.85)';
      if (!owner) ctx.setLineDash([4 * dpr, 3 * dpr]);
      ctx.stroke();
      ctx.setLineDash([]);
      caps.forEach(function (c, i) {  // capture progress, clockwise from the top
        circle(zone, r - i * 5 * dpr / scale, 0, TAU * Math.min(c[1], 100) / 100);
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
      var q = pt(v.X[k], v.Z[k], 0.6);
      if (!q) { pen = false; continue; }
      if (pen) ctx.lineTo(q.x, q.y);
      else { ctx.moveTo(q.x, q.y); pen = true; }
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
      var pen = path(v, from, stop), here = s && s.k === 'live' ? pt(s.x, s.z, 0.6) : null;
      if (pen && here) ctx.lineTo(here.x, here.y);
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
  function barrel(deg, r, color) {  // where the gun points: hull yaw + turret yaw
    ctx.save();
    ctx.rotate(deg * Math.PI / 180);
    ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(0, -.3 * r); ctx.lineTo(0, -1.25 * r - 9 * dpr);
    ctx.strokeStyle = INK; ctx.lineWidth = 3.6 * dpr; ctx.stroke();
    ctx.strokeStyle = color; ctx.lineWidth = 1.6 * dpr; ctx.stroke();
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
    var r = (v.s === 'm' ? 6 : 5) * dpr, c = COL[v.s], flat = MODE === '2d';
    var q = pt(s.x, s.z, flat ? 0 : 2);
    if (!q) return;
    ctx.save();
    ctx.translate(q.x, q.y);
    ctx.globalAlpha = focus && v !== focus ? .3 : 1;
    if (!flat && s.k !== 'ghost') {
      if (s.k === 'live' && v.s === 'm') ring(13 * dpr, 'rgba(255,255,255,.9)', 1.5);
    } else if (s.k === 'dead') {
      ctx.lineCap = 'round';
      ctx.strokeStyle = 'rgba(0,0,0,.65)'; ctx.lineWidth = 4.5 * dpr; cross(r * .8);
      ctx.strokeStyle = c; ctx.lineWidth = 2.2 * dpr; cross(r * .8);
    } else if (s.k === 'ghost') {
      ctx.globalAlpha *= Math.max(.3, .85 - s.age / 60);
      ctx.setLineDash([2.5 * dpr, 2 * dpr]);
      shape(v.c, r); ctx.strokeStyle = c; ctx.lineWidth = 1.6 * dpr; ctx.stroke();
      ctx.setLineDash([]);
    } else {
      if (s.g !== null) barrel(s.y + s.g, r, TEXT[v.s]);
      wedge(s.y, r, c);
      shape(v.c, r); ctx.fillStyle = c; ctx.fill();
      cuts(v.c, r);
      shape(v.c, r); ctx.strokeStyle = INK; ctx.lineWidth = 1.3 * dpr; ctx.stroke();
      if (v.s === 'm') ring(1.25 * r + 3.5 * dpr, 'rgba(255,255,255,.9)', 1.5);
    }
    if (v === focus) {
      var fr = flat ? 1.25 * r + 8 * dpr : 17 * dpr;
      ctx.globalAlpha = 1;
      ring(fr, 'rgba(0,0,0,.6)', 4);
      ring(fr, '#FFFFFF', 2);
    }
    ctx.restore();
  }
  function drawHp(v, s, t) {  // a health bar under a live tank (over it in 3D), as much of it as was left
    var hp = hpAt(v, t), q = pt(s.x, s.z, MODE === '2d' ? 0 : 4.5);
    if (hp === null || !v.hpMax || !q) return;
    var w = 18 * dpr, h = 3 * dpr, x = q.x - w / 2, y = q.y + (MODE === '2d' ? 9 : -6) * dpr;
    ctx.save();
    ctx.globalAlpha = focus && v !== focus ? .3 : 1;
    ctx.fillStyle = 'rgba(0,0,0,.75)'; ctx.fillRect(x - dpr, y - dpr, w + 2 * dpr, h + 2 * dpr);
    ctx.fillStyle = COL[v.s]; ctx.fillRect(x, y, w * Math.max(0, Math.min(1, hp / v.hpMax)), h);
    ctx.restore();
  }
  function drawLabel(v, s) {
    var picked = v === focus;
    if (labels === 'none' && !picked) return;
    var text = picked || labels === 'nick' ? v.n : v.k;
    if (s.k === 'ghost') text += ' · ' + Math.round(s.age) + ' с';
    var bar = s.k === 'live' && hpAt(v, cur) !== null ? 5 : 0, q = pt(s.x, s.z, MODE === '2d' ? 0 : 4.5);
    if (!q) return;
    var x = q.x, y = MODE === '2d' ? q.y + ((picked ? 16 : s.k === 'dead' ? 7 : 10) + bar) * dpr
                                  : q.y - (bar ? 21 : 15) * dpr;
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
  function arrow(x0, y0, x1, y1, color, dashed) {  // from the killer to the victim, clear of both marks
    var dx = x1 - x0, dy = y1 - y0, len = Math.hypot(dx, dy);
    if (len < 24 * dpr) return;
    var ux = dx / len, uy = dy / len, hs = 7 * dpr;
    var sx = x0 + ux * 9 * dpr, sy = y0 + uy * 9 * dpr, ex = x1 - ux * 11 * dpr, ey = y1 - uy * 11 * dpr;
    ctx.lineCap = 'round';
    if (dashed) ctx.setLineDash([5 * dpr, 4 * dpr]);
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex - ux * hs * .8, ey - uy * hs * .8);
    ctx.strokeStyle = INK; ctx.lineWidth = 4 * dpr; ctx.stroke();
    ctx.strokeStyle = color; ctx.lineWidth = 2 * dpr; ctx.stroke();
    ctx.setLineDash([]);
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
  function killingShot(killer, victim, t) {  // its tracer starts where a hidden killer stood
    for (var i = since(SHOTS, t - 1.5); i < SHOTS.length && SHOTS[i][0] <= t + 0.05; i++) {
      var s = SHOTS[i];
      if (s[1] === killer && s[6] === victim && !s[9]) return s;
    }
    return null;
  }
  function drawKills(t) {  // the first two seconds after each kill
    R.kills.forEach(function (k) {
      var age = t - k.t, victim = byId[k.v];
      if (age < 0 || age > 2 || !victim || !victim.T) return;
      var vs = state(victim, k.t + 0.05);
      if (!vs) return;
      var vq = pt(vs.x, vs.z, 1.5);
      if (!vq) return;
      var x = vq.x, y = vq.y, killer = k.k !== k.v ? byId[k.k] : null, ks = seen(killer, k.t);
      ctx.save();
      ctx.globalAlpha = 1 - age / 2;
      var shot = !ks && killer ? killingShot(killer.id, k.v, k.t) : null;
      var kq = ks ? pt(ks.x, ks.z, 1.5) : shot ? pt(shot[2], shot[3], 1.8) : null;
      if (kq) arrow(kq.x, kq.y, x, y, COL[killer.s], !ks);  // dashed: from the start of the shell
      else if (killer && killer.s === 'e') caption('стрелок не в засвете', x, y - 12 * dpr);
      ctx.globalAlpha = Math.max(0, 1 - age / 1.5);
      ctx.strokeStyle = '#FFFFFF'; ctx.lineWidth = 2 * dpr;
      ctx.beginPath(); ctx.arc(x, y, (8 + age * 22) * dpr, 0, TAU); ctx.stroke();
      ctx.restore();
    });
  }

  // ---- 3D: the terrain under the minimap, tanks as boxes, the overlays above ----
  // World x east / z north / heights in metres become GL x / y / -z, so a
  // camera at the south edge looks north with east to its right.
  var gl = null, glc = null, G3 = null, TER = null, VP = null;
  var cam = {tx: 0, tz: 0, yaw: 0, pitch: .8, dist: 450};
  var BIG = 1.8;  // tanks drawn this much larger than life, or a 7 m hull is a dot from 400 m
  var viewBtn = card.querySelector('.seg[data-group="view"]'), note3d = document.getElementById('rp-3dnote');
  function ground(x, z) {  // terrain height at a point, bilinear; 0 before the terrain is loaded
    if (!TER) return 0;
    var w = R.world, n = TER.n, h = TER.h;
    var u = (x - w[0]) / (w[2] - w[0]) * (n - 1), v = (z - w[1]) / (w[3] - w[1]) * (n - 1);
    var i = Math.max(0, Math.min(n - 2, Math.floor(u))), j = Math.max(0, Math.min(n - 2, Math.floor(v)));
    var fu = Math.max(0, Math.min(1, u - i)), fv = Math.max(0, Math.min(1, v - j));
    var a = h[j * n + i] * (1 - fu) + h[j * n + i + 1] * fu;
    return a * (1 - fv) + (h[(j + 1) * n + i] * (1 - fu) + h[(j + 1) * n + i + 1] * fu) * fv;
  }
  function loadTerrain(done) {  // the heights ship zlib-compressed: inflate once, in the browser
    if (TER) { done(true); return; }
    if (!R.terrain || typeof DecompressionStream === 'undefined') { done(false); return; }
    var bin = atob(R.terrain.dz), bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    var stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
    new Response(stream).arrayBuffer().then(function (buf) {
      var n = R.terrain.n, steps = new Int16Array(buf), h = new Float32Array(n * n);
      for (var j = 0; j < n; j++) {
        var value = 0;  // each row: its first height, then steps; decimetres
        for (var k = 0; k < n; k++) {
          value = k ? value + steps[j * n + k] : steps[j * n];
          h[j * n + k] = value / 10;
        }
      }
      TER = {n: n, h: h};
      done(true);
    }, function () { done(false); });
  }

  function mat(m) { return new Float32Array(m); }  // column-major 4x4
  function mul(a, b) {
    var o = new Float32Array(16);
    for (var c = 0; c < 4; c++) for (var r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
    return o;
  }
  function perspective(fov, aspect, near, far) {
    var f = 1 / Math.tan(fov / 2), d = near - far;
    return mat([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / d, -1, 0, 0, 2 * far * near / d, 0]);
  }
  function lookAt(e, c) {  // y up
    var zx = e[0] - c[0], zy = e[1] - c[1], zz = e[2] - c[2], zl = Math.hypot(zx, zy, zz);
    zx /= zl; zy /= zl; zz /= zl;
    var xx = zz, xz = -zx, xl = Math.hypot(xx, xz) || 1;  // up x z, with up = (0, 1, 0)
    xx /= xl; xz /= xl;
    var yx = zy * xz, yy = zz * xx - zx * xz, yz = -zy * xx;
    return mat([xx, yx, zx, 0, 0, yy, zy, 0, xz, yz, zz, 0,
                -(xx * e[0] + xz * e[2]), -(yx * e[0] + yy * e[1] + yz * e[2]), -(zx * e[0] + zy * e[1] + zz * e[2]), 1]);
  }
  function tr(x, y, z) { return mat([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, x, y, z, 1]); }
  function rotY(a) { var c = Math.cos(a), s = Math.sin(a); return mat([c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0, 0, 0, 0, 1]); }
  function sc(x, y, z) { return mat([x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1]); }
  function project(x, y, z) {  // world point -> canvas pixels, null behind the camera or far off screen
    if (!VP) return null;
    var gz = -z, w = VP[3] * x + VP[7] * y + VP[11] * gz + VP[15];
    if (w < 1) return null;
    var sx = (VP[0] * x + VP[4] * y + VP[8] * gz + VP[12]) / w, sy = (VP[1] * x + VP[5] * y + VP[9] * gz + VP[13]) / w;
    if (Math.abs(sx) > 3 || Math.abs(sy) > 3) return null;
    return {x: (sx + 1) / 2 * W, y: (1 - sy) / 2 * H};
  }

  var VS_TER = 'attribute vec3 p;attribute vec3 nm;attribute vec2 uv;uniform mat4 mvp;' +
    'varying vec2 vuv;varying vec3 vn;varying vec2 vw;' +
    'void main(){vuv=uv;vn=nm;vw=vec2(p.x,-p.z);gl_Position=mvp*vec4(p,1.0);}';
  var FS_TER = 'precision mediump float;uniform sampler2D tex;uniform vec4 bd;uniform vec3 sun;' +
    'varying vec2 vuv;varying vec3 vn;varying vec2 vw;' +
    'void main(){vec3 c=texture2D(tex,vuv).rgb;float l=.55+.6*max(dot(normalize(vn),sun),0.0);' +
    'float inside=step(bd.x,vw.x)*step(vw.x,bd.z)*step(bd.y,vw.y)*step(vw.y,bd.w);' +
    'gl_FragColor=vec4(c*l*mix(.5,1.0,inside),1.0);}';
  var VS_BOX = 'attribute vec3 p;attribute vec3 nm;uniform mat4 mvp;uniform mat4 model;varying vec3 vn;' +
    'void main(){vn=mat3(model[0].xyz,model[1].xyz,model[2].xyz)*nm;gl_Position=mvp*model*vec4(p,1.0);}';
  var FS_BOX = 'precision mediump float;uniform vec4 col;uniform vec3 sun;varying vec3 vn;' +
    'void main(){float l=.5+.65*max(dot(normalize(vn),sun),0.0);gl_FragColor=vec4(col.rgb*l,col.a);}';
  function program(vs, fs) {
    var p = gl.createProgram();
    [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]].forEach(function (pair) {
      var sh = gl.createShader(pair[0]);
      gl.shaderSource(sh, pair[1]); gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
      gl.attachShader(p, sh);
    });
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    var info = {p: p, a: {}, u: {}};
    ['p', 'nm', 'uv'].forEach(function (name) { info.a[name] = gl.getAttribLocation(p, name); });
    ['mvp', 'model', 'tex', 'bd', 'sun', 'col'].forEach(function (name) { info.u[name] = gl.getUniformLocation(p, name); });
    return info;
  }
  function buffer(data, kind) {
    var b = gl.createBuffer();
    gl.bindBuffer(kind || gl.ARRAY_BUFFER, b);
    gl.bufferData(kind || gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    return b;
  }
  function terrainMesh() {  // one vertex per height: position, normal, minimap uv
    var n = TER.n, h = TER.h, w = R.world, c = R.crop, iw = img.naturalWidth || 512, ih = img.naturalHeight || 512;
    var dx = (w[2] - w[0]) / (n - 1), dz = (w[3] - w[1]) / (n - 1);
    var pos = new Float32Array(n * n * 3), nrm = new Float32Array(n * n * 3), uv = new Float32Array(n * n * 2);
    for (var j = 0; j < n; j++) for (var i = 0; i < n; i++) {
      var k = j * n + i, x = w[0] + i * dx, z = w[1] + j * dz;
      pos[3 * k] = x; pos[3 * k + 1] = h[k]; pos[3 * k + 2] = -z;
      var hx = h[j * n + Math.min(n - 1, i + 1)] - h[j * n + Math.max(0, i - 1)];
      var hz = h[Math.min(n - 1, j + 1) * n + i] - h[Math.max(0, j - 1) * n + i];
      var nx = -hx / (2 * dx), nz = hz / (2 * dz), nl = Math.hypot(nx, 1, nz);  // GL z runs south
      nrm[3 * k] = nx / nl; nrm[3 * k + 1] = 1 / nl; nrm[3 * k + 2] = nz / nl;
      uv[2 * k] = (c[0] + i / (n - 1) * (c[2] - c[0])) / iw;
      uv[2 * k + 1] = (c[1] + (1 - j / (n - 1)) * (c[3] - c[1])) / ih;
    }
    var idx = new Uint16Array((n - 1) * (n - 1) * 6), m = 0;
    for (var jj = 0; jj < n - 1; jj++) for (var ii = 0; ii < n - 1; ii++) {
      var a = jj * n + ii;
      idx[m++] = a; idx[m++] = a + 1; idx[m++] = a + n; idx[m++] = a + 1; idx[m++] = a + n + 1; idx[m++] = a + n;
    }
    return {pos: buffer(pos), nrm: buffer(nrm), uv: buffer(uv), idx: buffer(idx, gl.ELEMENT_ARRAY_BUFFER), count: m};
  }
  function boxMesh() {  // a unit box standing on y = 0, centred in x and z
    var f = [[0, 1, 0], [0, -1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1]], pos = [], nrm = [], idx = [];
    f.forEach(function (n, k) {
      var u = n[1] ? [1, 0, 0] : [0, 1, 0], v = [n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0]];
      [[-1, -1], [1, -1], [1, 1], [-1, 1]].forEach(function (q) {
        pos.push(.5 * (n[0] + q[0] * u[0] + q[1] * v[0]), .5 * (n[1] + q[0] * u[1] + q[1] * v[1]) + .5,
                 .5 * (n[2] + q[0] * u[2] + q[1] * v[2]));
        nrm.push(n[0], n[1], n[2]);
      });
      idx.push(4 * k, 4 * k + 1, 4 * k + 2, 4 * k, 4 * k + 2, 4 * k + 3);
    });
    return {pos: buffer(new Float32Array(pos)), nrm: buffer(new Float32Array(nrm)),
            idx: buffer(new Uint16Array(idx), gl.ELEMENT_ARRAY_BUFFER), count: idx.length};
  }
  function initGL() {  // false when the browser has no WebGL
    glc = document.createElement('canvas');
    glc.className = 'rp-gl';
    stage.insertBefore(glc, cv);
    gl = glc.getContext('webgl', {antialias: true}) || glc.getContext('experimental-webgl');
    if (!gl) return false;
    try {
      G3 = {ter: program(VS_TER, FS_TER), box: program(VS_BOX, FS_BOX), mesh: terrainMesh(), cube: boxMesh(),
            tex: gl.createTexture()};
    } catch (err) {
      gl = null;
      return false;
    }
    gl.bindTexture(gl.TEXTURE_2D, G3.tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
    gl.generateMipmap(gl.TEXTURE_2D);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    var b = R.border || view;
    cam.tx = (b[0] + b[2]) / 2; cam.tz = (b[1] + b[3]) / 2;
    cam.dist = Math.max(b[2] - b[0], b[3] - b[1]) * .9;
    return true;
  }
  function bind(prog, mesh) {
    gl.useProgram(prog.p);
    [['p', mesh.pos, 3], ['nm', mesh.nrm, 3], ['uv', mesh.uv, 2]].forEach(function (a) {
      var loc = prog.a[a[0]];
      if (loc < 0 || loc === undefined) return;
      if (!a[1]) { gl.disableVertexAttribArray(loc); return; }
      gl.bindBuffer(gl.ARRAY_BUFFER, a[1]);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, a[2], gl.FLOAT, false, 0, 0);
    });
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, mesh.idx);
  }
  var SUN = [.45, .8, .38], DARK = [.22, .23, .25, 1];
  function rgb(side) { var c = RGB[side].split(','); return [c[0] / 255, c[1] / 255, c[2] / 255, 1]; }
  function box(model, color) {
    gl.uniformMatrix4fv(G3.box.u.model, false, model);
    gl.uniform4fv(G3.box.u.col, color);
    gl.drawElements(gl.TRIANGLES, G3.cube.count, gl.UNSIGNED_SHORT, 0);
  }
  function drawGL(states) {
    if (glc.width !== W || glc.height !== H) { glc.width = W; glc.height = H; }
    gl.viewport(0, 0, W, H);
    gl.clearColor(.05, .06, .07, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    if (focus) {  // the camera follows the picked tank
      var fs = states[V.indexOf(focus)];
      if (fs) { cam.tx = fs.x; cam.tz = fs.z; }
    }
    var ty = ground(cam.tx, cam.tz), cp = Math.cos(cam.pitch);
    var eye = [cam.tx - Math.sin(cam.yaw) * cp * cam.dist, ty + Math.sin(cam.pitch) * cam.dist,
               -cam.tz + Math.cos(cam.yaw) * cp * cam.dist];
    VP = mul(perspective(.75, W / H, 2, 4000), lookAt(eye, [cam.tx, ty, -cam.tz]));
    bind(G3.ter, G3.mesh);
    gl.uniformMatrix4fv(G3.ter.u.mvp, false, VP);
    var b = R.border || R.world;
    gl.uniform4f(G3.ter.u.bd, b[0], b[1], b[2], b[3]);
    gl.uniform3fv(G3.ter.u.sun, SUN);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, G3.tex);
    gl.uniform1i(G3.ter.u.tex, 0);
    gl.drawElements(gl.TRIANGLES, G3.mesh.count, gl.UNSIGNED_SHORT, 0);
    bind(G3.box, G3.cube);
    gl.uniformMatrix4fv(G3.box.u.mvp, false, VP);
    gl.uniform3fv(G3.box.u.sun, SUN);
    V.forEach(function (v, i) {
      var st = states[i];
      if (!st || st.k === 'ghost') return;
      var k = BIG * (v.c === 'LT' ? .85 : v.c === 'HT' ? 1.12 : 1), base = mul(tr(st.x, ground(st.x, st.z), -st.z),
          mul(rotY(-st.y * Math.PI / 180), sc(k, k, k)));
      var dim = focus && v !== focus, col = st.k === 'dead' ? DARK : rgb(v.s);
      if (dim) col = [col[0] * .55, col[1] * .55, col[2] * .55, 1];
      box(mul(base, sc(3.3, 1.5, 6.6)), col);
      if (st.k === 'dead') return;
      var turret = mul(base, mul(tr(0, 1.5, .4), rotY(-(st.g || 0) * Math.PI / 180)));
      box(mul(turret, sc(2.5, 1.05, 2.8)), col);
      box(mul(turret, mul(tr(0, .35, -3.4), sc(.38, .38, 4.6))), [col[0] * .8, col[1] * .8, col[2] * .8, 1]);
    });
  }
  function setView(mode) {
    Array.prototype.forEach.call(viewBtn.querySelectorAll('button'), function (x) {
      x.classList.toggle('on', x.getAttribute('data-v') === mode);
    });
    if (mode === '2d') {
      MODE = '2d'; stage.classList.remove('is3d');
      if (glc) glc.hidden = true;
      note3d.hidden = true; draw(); return;
    }
    note3d.hidden = false;
    note3d.textContent = 'Загружаю рельеф…';
    loadTerrain(function (ok) {
      if (ok && !gl && !G3) ok = initGL();
      if (!ok || !gl) {
        note3d.textContent = 'Этот браузер не умеет 3D (WebGL) — остаётся 2D.';
        setView('2d'); note3d.hidden = false;
        return;
      }
      note3d.textContent = 'Тяните — поворот, колесо или щипок — ближе/дальше, правая кнопка или Shift — сдвиг.';
      MODE = '3d'; glc.hidden = false; stage.classList.add('is3d'); draw();
    });
  }
  var drag = null, dragged = false, touches = {};
  function pinch() {
    var ids = Object.keys(touches);
    return ids.length === 2 ? Math.hypot(touches[ids[0]].x - touches[ids[1]].x, touches[ids[0]].y - touches[ids[1]].y) : 0;
  }
  stage.addEventListener('pointerdown', function (e) {
    if (MODE !== '3d') return;
    touches[e.pointerId] = {x: e.clientX, y: e.clientY};
    drag = {x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey, span: pinch()};
    dragged = false;
    if (stage.setPointerCapture) stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener('pointermove', function (e) {
    if (MODE !== '3d' || !drag || !touches[e.pointerId]) return;
    touches[e.pointerId] = {x: e.clientX, y: e.clientY};
    var dx = e.clientX - drag.x, dy = e.clientY - drag.y, span = pinch();
    if (Math.abs(dx) + Math.abs(dy) > 3 || span) dragged = true;
    if (!dragged) return;
    if (span && drag.span) {
      cam.dist = Math.max(40, Math.min(900, cam.dist * drag.span / span));
      drag.span = span;
    } else if (drag.pan) {
      // the ground moves with the pointer: right is (cos, -sin), forward (sin, cos)
      var k = cam.dist * .79 / (stage.clientHeight || 600), s = Math.sin(cam.yaw), c = Math.cos(cam.yaw);
      cam.tx += (-c * dx + s * dy) * k; cam.tz += (s * dx + c * dy) * k;
    } else {
      cam.yaw += dx * .006;
      cam.pitch = Math.max(.2, Math.min(1.5, cam.pitch + dy * .005));
    }
    drag.x = e.clientX; drag.y = e.clientY;
    draw();
  });
  function release(e) {
    delete touches[e.pointerId];
    if (!Object.keys(touches).length) drag = null;
  }
  stage.addEventListener('pointerup', release);
  stage.addEventListener('pointercancel', release);
  stage.addEventListener('wheel', function (e) {
    if (MODE !== '3d') return;
    e.preventDefault();
    cam.dist = Math.max(40, Math.min(900, cam.dist * Math.exp(e.deltaY * .0012)));
    draw();
  }, {passive: false});
  stage.addEventListener('contextmenu', function (e) { if (MODE === '3d') e.preventDefault(); });

  function lasting(base) { return Math.max(base, .3 * speed); }  // battle seconds an effect stays on screen
  function drawSpotted(v, s, t) {  // a ring that shrinks onto an enemy as it is spotted
    var span = lasting(1.2), i = at(v.spotted, t);
    if (i < 0 || t - v.spotted[i] > span || t < 0) return;
    var age = (t - v.spotted[i]) / span;
    ctx.save();
    ctx.globalAlpha = (1 - age) * (focus && v !== focus ? .35 : 1);
    var q = pt(s.x, s.z, MODE === '2d' ? 0 : 2);
    if (!q) { ctx.restore(); return; }
    ctx.beginPath(); ctx.arc(q.x, q.y, (24 - 12 * age) * dpr, 0, TAU);
    ctx.strokeStyle = 'rgba(0,0,0,.6)'; ctx.lineWidth = 4 * dpr; ctx.stroke();
    ctx.strokeStyle = COL.e; ctx.lineWidth = 2 * dpr; ctx.stroke();
    ctx.restore();
  }
  function drawShots(t) {  // tracers, fading out; a ricochet's flight on is dashed
    var span = lasting(.8);
    for (var i = since(SHOTS, t - span); i < SHOTS.length && SHOTS[i][0] <= t; i++) {
      var s = SHOTS[i], shooter = byId[s[1]], target = byId[s[6]];
      if (s[4] === null) continue;
      var a = pt(s[2], s[3], 1.8), b = pt(s[4], s[5], s[6] ? 1.5 : 0.3);
      if (!a || !b) continue;
      ctx.save();
      ctx.globalAlpha = (1 - (t - s[0]) / span) * (focus && shooter !== focus && target !== focus ? .2 : 1);
      ctx.lineCap = 'round';
      if (s[9]) ctx.setLineDash([3 * dpr, 3 * dpr]);
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
      ctx.strokeStyle = INK; ctx.lineWidth = 3 * dpr; ctx.stroke();
      ctx.strokeStyle = shooter ? TEXT[shooter.s] : '#FFFFFF'; ctx.lineWidth = 1.4 * dpr; ctx.stroke();
      ctx.restore();
    }
  }
  var WORD = {0: 'рикошет', 1: 'рикошет', 2: 'не пробил', 4: 'крит'};  // a hit without damage
  var REASON = {1: 'пожар', 2: 'таран', 3: 'падение'};
  function floater(v, t0, t, span, text, color, stack) {  // rises over the tank and fades
    var s = seen(v, t) || seen(v, t0), q = s && pt(s.x, s.z, MODE === '2d' ? 0 : 5);
    if (!q) return;
    var age = (t - t0) / span, x = q.x, y = q.y - ((MODE === '2d' ? 15 : 34) + 14 * age + 13 * stack) * dpr;
    ctx.save();
    ctx.globalAlpha = (1 - age) * (focus && v !== focus ? .35 : 1);
    ctx.font = '700 ' + Math.round(12 * dpr) + 'px ' + FONT;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.lineJoin = 'round';
    ctx.lineWidth = 3 * dpr; ctx.strokeStyle = 'rgba(0,0,0,.85)'; ctx.strokeText(text, x, y);
    ctx.fillStyle = color; ctx.fillText(text, x, y);
    ctx.restore();
  }
  function drawHits(t) {  // damage numbers -- warm on enemies, red on allies -- and hits without damage
    var span = lasting(1.4), stack = {};
    function slot(id) { stack[id] = (stack[id] || 0) + 1; return stack[id] - 1; }
    for (var i = since(HITS, t - span); i < HITS.length && HITS[i][0] <= t; i++) {
      var h = HITS[i], v = byId[h[2]], text = h[4] > 0 ? '−' + h[4] : WORD[h[3]];
      if (!v || !v.T || !text) continue;
      floater(v, h[0], t, span, text, h[4] > 0 ? (v.s === 'e' ? '#FFD27A' : '#FF8A7A') : '#C9CED3', slot(v.id));
    }
    for (var j = since(DMG, t - span); j < DMG.length && DMG[j][0] <= t; j++) {
      var d = DMG[j], w = byId[d[2]];
      if (!d[4] || !w || !w.T) continue;  // shells came with their hits above
      floater(w, d[0], t, span, '−' + d[3] + ' ' + (REASON[d[4]] || ''), '#FFB08A', slot(w.id));
    }
  }

  function render() {
    ctx.clearRect(0, 0, W, H);
    var states = V.map(function (v) { return state(v, cur); });
    if (MODE === '3d') drawGL(states); else drawMap();
    drawZones(cur);
    V.forEach(function (v, i) { drawTrail(v, states[i], cur); });
    ['dead', 'ghost', 'live'].forEach(function (kind) {
      V.forEach(function (v, i) {
        if (v !== focus && states[i] && states[i].k === kind) drawTank(v, states[i]);
      });
    });
    var f = focus ? V.indexOf(focus) : -1;
    if (f >= 0 && states[f]) drawTank(focus, states[f]);  // the picked tank on top
    V.forEach(function (v, i) {
      if (states[i] && states[i].k === 'live') { drawHp(v, states[i], cur); if (v.spotted.length) drawSpotted(v, states[i], cur); }
    });
    drawShots(cur);
    V.forEach(function (v, i) { if (states[i]) drawLabel(v, states[i]); });
    drawKills(cur);  // over the labels: it only lasts two seconds
    drawHits(cur);
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
  var liveEl = null, liveSig = '';
  function showLive(t) {  // the picked tank up to the replay's time: health, damage, shots
    if (!liveEl || !focus) return;
    var v = focus, hp = hpAt(v, t), dealt = 0, taken = 0, fired = 0, landed = 0;
    DMG.forEach(function (d) {
      if (d[0] > t) return;
      if (d[1] === v.id && d[2] !== v.id) dealt += d[3];
      if (d[2] === v.id) taken += d[3];
    });
    SHOTS.forEach(function (s) {
      if (s[0] <= t && s[1] === v.id && !s[9]) { fired++; if (s[6]) landed++; }
    });
    var sig = [fmt(t), hp, dealt, taken, fired, landed].join('|');
    if (sig === liveSig) return;
    liveSig = sig;
    liveEl.textContent = '';
    liveEl.appendChild(el('span', 'rc-lbl', 'к ' + fmt(t)));
    if (hp !== null && v.hpMax) {
      var bar = el('span', 'rc-hp'), fill = el('i');
      fill.style.width = Math.max(0, Math.min(100, 100 * hp / v.hpMax)) + '%';
      fill.style.background = COL[v.s];
      bar.appendChild(fill);
      liveEl.appendChild(bar);
    }
    var row = el('div', 'rc-stats');
    [[hp === null ? '—' : num(hp) + (v.hpMax ? ' / ' + num(v.hpMax) : ''), 'HP'], [num(dealt), 'урон'],
     [num(taken), 'получено'], [fired + (fired ? ' (' + landed + ')' : ''), 'выстр. (в цель)']].forEach(function (p) {
      var item = el('span');
      item.appendChild(el('b', null, p[0]));
      item.appendChild(document.createTextNode(' ' + p[1]));
      row.appendChild(item);
    });
    liveEl.appendChild(row);
    if (v.s === 'e' && hp !== null && !seen(v, t) && (v.d === null || t < v.d)) {
      liveEl.appendChild(el('div', 'rc-note', 'Сейчас не в засвете: HP — как видели в последний раз.'));
    }
  }
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
    liveEl = EV ? el('div', 'rc-live') : null;
    liveSig = '';
    if (liveEl) { cardEl.appendChild(liveEl); showLive(cur); }
    if (v.r) {
      var stats = el('div', 'rc-stats');
      stats.appendChild(el('span', 'rc-lbl', 'итог боя'));
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
    showLive(cur);
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
      var q = pt(s.x, s.z, MODE === '2d' ? 0 : 1.5);
      if (!q) return;
      var d = Math.hypot(q.x - x, q.y - y) + (s.k === 'live' ? 0 : 6 * dpr);
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
    if (dragged) { dragged = false; return; }  // that was the 3D camera
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
      if (g === 'view') { setView(value); return; }
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
    body = (_hero(battle) + _me(battle) + _coach(battle) + _fight(battle) + _replay(battle)
            + f'<div class="teams">{_team(battle, True, leaders)}{_team(battle, False, leaders)}</div>'
            + ("" if has_replay else _feed(battle)) + _footer(battle))
    script = _SCRIPT + (_REPLAY_SCRIPT if has_replay else "")
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="{theme.GOOGLE_FONTS}">'
        f'<style>{_CSS}</style></head><body>{site_nav}'
        f'<main class="wrap">{body}</main><script>{script}</script></body></html>'
    )
