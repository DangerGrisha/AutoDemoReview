"""HTML report for one Blitz battle -- self-contained (inline CSS, a few lines of JS).

Every string that comes from a replay (nicknames, clan tags) is controlled by
whoever made the file and goes through `esc`; numbers are formatted, never
interpolated raw. The minimap is embedded as a data: URI, so a saved page
still renders. `site_nav` is trusted HTML built by the web layer.
"""

import base64
import html
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


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #

def _hero(battle):
    label, cls = OUTCOME.get(battle["outcome"], OUTCOME["unknown"])
    minimap = _minimap_uri(battle["map"].get("key"))
    img = (f'<img src="{minimap}" alt="Миникарта: {esc(battle["map"]["name"])}">'
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
    when = f" на {clock(me['death_time'])}" if me.get("death_time") is not None else ""
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


def _feed(battle):
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
        items.append(f'<li><span class="t">{clock(kill["t"])}</span><span>{who} → '
                     f'<span class="{_side_class(victim)}">{esc(victim["nickname"])}</span> '
                     f'<span class="tank">{esc(victim["tank"]["short"])}</span></span></li>')
    body = (f'<ul class="feed">{"".join(items)}</ul>' if items
            else '<p class="note">В записи уничтожений нет.</p>')
    return f'<section class="card"><h2>Уничтожения</h2>{body}</section>'


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


def render_battle_html(battle, site_nav=""):
    """The full report page for one battle dict (see battle.load)."""
    label, _ = OUTCOME.get(battle["outcome"], OUTCOME["unknown"])
    title = f'{battle["map"]["name"]} — {label} · Blitz · demoReview'
    leaders = analysis.leaders(battle)
    body = (_hero(battle) + _me(battle)
            + f'<div class="teams">{_team(battle, True, leaders)}{_team(battle, False, leaders)}</div>'
            + _feed(battle) + _footer(battle))
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(title)}</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;'
        '600;700&family=JetBrains+Mono:wght@400;600&family=Rajdhani:wght@600;700&display=swap">'
        f'<style>{_CSS}</style></head><body>{site_nav}'
        f'<main class="wrap">{body}</main><script>{_SCRIPT}</script></body></html>'
    )
