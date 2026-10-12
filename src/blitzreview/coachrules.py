"""The free coach for Blitz: a rule engine over the fact sheet (tactics.py).

Each rule knows one failure mode, when it fires, how badly, and what to say.
The report has the shape of the CS2 coach's (demoreview/coachrules.py):

    {"headline": str,
     "strengths": [{"point", "evidence"}],
     "problems":  [{"code", "title", "severity", "cause", "evidence",
                    "moments": [battle seconds], "fix"}],
     "drill":     {"name", "how", "target"}}

`moments` are times the report links into the 2D replay. A rule cites only
what the fact sheet holds -- every number in its evidence comes from there --
and skips itself when a fact is missing (no battle results, a battle stored
before the events were decoded) instead of guessing.

What a rule engine cannot do, and does not pretend to: notice a pattern
nobody wrote a rule for. It never invents anything and costs nothing.
Russian only: these are hand-written strings, not translations.
"""

from . import tactics

# Bottom three of the lobby is a real problem at the player's own level.
BAD_FROM_BOTTOM = 2   # rank >= of - 2

# Inherent weight of each failure mode, independent of how badly it scored.
WEIGHT = {
    "early_death": 100,
    "cheap_death": 90,
    "isolated_death": 85,
    "standing": 80,
    "alone_start": 70,
    "focus": 65,
    "misses": 55,
    "no_pen": 50,
    "passive": 45,
}

DRILLS = {
    "early_death": ("Первые две минуты", "В начале боя не выезжайте первым: займите позицию "
                    "за укрытием, стреляйте по тем, кого засветили союзники, и меняйте место после "
                    "выстрела.", "Дожить хотя бы до середины боя в следующих пяти боях."),
    "cheap_death": ("Размен в плюс", "Перед каждым выездом спросите себя: успею ли выстрелить и "
                    "уйти до ответа? Выезжайте только с готовым выстрелом и только на одного "
                    "противника.", "Нанести до гибели не меньше урона, чем у вас прочности."),
    "isolated_death": ("Рядом со своими", "Раз в 20–30 секунд смотрите на миникарту: если "
                       "ближайший союзник дальше 100 м, а противников рядом больше одного — "
                       "отходите к своим заранее.", "Ни одной гибели дальше 150 м от союзника."),
    "standing": ("Выстрел — откат", "Выкатились, выстрелили — сразу за укрытие на перезарядку. "
                 "Получили попадание — сдвиньтесь, не ждите второго.",
                 "Ни разу не получить два пробития подряд, стоя на месте."),
    "alone_start": ("Направление с командой", "На старте посмотрите, куда едут союзники, и "
                    "выберите фланг, где будет хотя бы ещё 1–2 танка.",
                    "На первой минуте боя держаться в 100 м от союзника."),
    "focus": ("Один на один", "Выезжайте так, чтобы вас видел только один противник: углы "
              "зданий, обратные скаты. Двое и больше — не выезжайте.",
              "Не попадать под огонь трёх противников сразу."),
    "misses": ("Сведение", "Стреляйте после полного сведения прицела; по движущимся целям — с "
               "упреждением, по дальним — в центр силуэта.",
               "Точность не ниже медианы лобби."),
    "no_pen": ("Уязвимые зоны", "Целитесь в нижний бронелист, борта и стык башни; против "
               "крепкой брони заранее переключайтесь на бронебойный или кумулятивный снаряд.",
               "Пробивать не меньше двух попаданий из трёх."),
    "passive": ("Позиция для стрельбы", "Ищите место, откуда можно стрелять, не подставляясь: "
                "живой танк без урона команде не помогает.",
                "Нанести урона хотя бы на половину своей прочности."),
}

SEVERITY_ORDER = {"high": 2, "medium": 1, "low": 0}


def _plu(n, one, few, many):
    """Russian noun agreement for a count (1 выстрел / 2 выстрела / 5 выстрелов)."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _clock(seconds):
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _num(value):
    text = f"{int(round(value)):,}".replace(",", " ")
    return text.replace("-", "−")


def _signed(value):
    return ("+" if value > 0 else "") + _num(value)


def _pct(value):
    return f"{round(100 * value)}%"


def _bad(rank):
    return rank["rank"] >= rank["of"] - BAD_FROM_BOTTOM


def _names(facts, ids):
    out = []
    for eid in ids:
        who = facts["names"].get(eid)
        if who:
            out.append(f"{who['name']} ({who['tank']})")
    return ", ".join(out)


# --------------------------------------------------------------------------- #
# rules -- each returns a problem dict or None
# --------------------------------------------------------------------------- #

def _rule_early_death(f):
    d = f["death"]
    if not d or not f["duration"] or d["order"] > 3 or d["t"] > 0.5 * f["duration"]:
        return None
    first = d["order"] == 1
    return {
        "code": "early_death",
        "title": "Вы погибли первым в бою" if first else "Вы погибли одним из первых",
        "severity": "high" if first else "medium",
        "cause": "В начале боя вы оказались под огнём раньше остальных: выехали туда, где "
                 "противник уже ждал, или раньше, чем союзники могли поддержать.",
        "evidence": f"Уничтожены на {_clock(d['t'])} — {d['order']}-м из {d['of']} танков боя, "
                    f"когда до конца боя оставалось больше половины.",
        "moments": [d["t"] - 8],
        "fix": "Первые полторы-две минуты не выезжайте первым: стреляйте по тем, кого засветили "
               "союзники, и после выстрела меняйте позицию.",
    }


def _rule_cheap_death(f):
    d, me = f["death"], f["me"]
    if not d or d["dealt"] is None or not me["max_hp"] or d["dealt"] >= 0.5 * me["max_hp"]:
        return None
    return {
        "code": "cheap_death",
        "title": "Прочность отдана почти даром",
        "severity": "high" if d["dealt"] < 0.25 * me["max_hp"] else "medium",
        "cause": "Противники сняли с вас всю прочность, а вы успели ответить лишь на малую её "
                 "часть: перестрелки шли не на ваших условиях.",
        "evidence": f"До гибели нанесено {_num(d['dealt'])} урона при {_num(me['max_hp'])} HP "
                    f"прочности — размен {_signed(d['dealt'] - me['max_hp'])}.",
        "moments": [d["t"] - 8],
        "fix": "Выезжайте только с готовым выстрелом и только на одного противника; после "
               "выстрела — за укрытие, пока перезаряжаетесь.",
    }


def _rule_isolated_death(f):
    d = f["death"]
    if not d or d["nearest_ally_m"] is None or d["nearest_ally_m"] <= tactics.ISOLATED_M:
        return None
    shooters = ""
    if d["attackers"]:
        n = len(d["attackers"])
        shooters = (f" За последние {int(tactics.FOCUS_WINDOW)} с по вам стреляли {n} "
                    f"{_plu(n, 'противник', 'противника', 'противников')}: "
                    f"{_names(f, d['attackers'])}.")
    return {
        "code": "isolated_death",
        "title": "Погибли в отрыве от команды",
        "severity": "high" if d["nearest_ally_m"] > 250 else "medium",
        "cause": "Рядом не было никого, кто мог бы стрелять по тем же целям: противники "
                 "разобрали вас, не опасаясь ответа союзников.",
        "evidence": f"Когда вас уничтожили ({_clock(d['t'])}), ближайший живой союзник был "
                    f"в {_num(d['nearest_ally_m'])} м.{shooters}",
        "moments": [d["t"] - 10],
        "fix": "Держитесь так, чтобы хотя бы один союзник мог стрелять по тем же целям — "
               "до 100 м. Пустеет фланг — отходите к своим раньше, чем вас окружат.",
    }


def _rule_focus(f):
    d = f["death"]
    if not d or not d["attackers"] or len(d["attackers"]) < 3:
        return None
    if d["nearest_ally_m"] is not None and d["nearest_ally_m"] > tactics.ISOLATED_M:
        return None  # said by the isolated death, with the same names
    n = len(d["attackers"])
    return {
        "code": "focus",
        "title": "Попали под огонь сразу нескольких противников",
        "severity": "high" if n >= 4 else "medium",
        "cause": "Позиция, где вы погибли, простреливалась сразу с нескольких направлений: "
                 "каждый выезд встречали несколько стволов.",
        "evidence": f"За последние {int(tactics.FOCUS_WINDOW)} с до гибели по вам стреляли "
                    f"{n} {_plu(n, 'противник', 'противника', 'противников')}: "
                    f"{_names(f, d['attackers'])}; потеряно {_num(d['lost_in_window'])} HP.",
        "moments": [d["t"] - tactics.FOCUS_WINDOW],
        "fix": "Выезжайте так, чтобы вас видел только один противник: углы зданий, обратные "
               "скаты. Если смотрят двое и больше — меняйте позицию.",
    }


def _rule_standing(f):
    spells = f["standing"]
    if not spells:
        return None
    hits = sum(s["hits"] for s in spells)
    damage = sum(s["damage"] for s in spells)
    max_hp = f["me"]["max_hp"]
    spans = ", ".join(f"{_clock(s['t0'])}–{_clock(s['t1'])}" for s in spells[:3])
    return {
        "code": "standing",
        "title": "Стояли на месте под огнём",
        "severity": "high" if max_hp and damage >= 0.4 * max_hp else "medium",
        "cause": "Получив попадание, вы оставались на месте и не стреляли в ответ: по "
                 "стоящему танку противник бьёт без упреждения.",
        "evidence": f"{hits} {_plu(hits, 'пробитие', 'пробития', 'пробитий')} "
                    f"({_num(damage)} урона) пришлось на моменты, когда вы не двигались и не "
                    f"стреляли: {spans}.",
        "moments": [s["t0"] - 3 for s in spells[:3]],
        "fix": "Получили попадание — сдвиньтесь: назад за укрытие, если перезаряжаетесь, "
               "вперёд-в-бок, если сближаетесь. Выстрел — и сразу откат.",
    }


def _rule_alone_start(f):
    a = f["alone_at_start"]
    if not a or a["nearest_ally_m"] <= tactics.ALONE_M or f["me"]["class"] == "LT":
        return None  # scouting alone is a light tank's job
    return {
        "code": "alone_start",
        "title": "В начале боя уехали в одиночку",
        "severity": "medium",
        "cause": "Вы выбрали направление, куда не поехал никто из команды: первого же "
                 "противника на фланге пришлось встречать одному.",
        "evidence": f"На {_clock(a['t'])} ближайший союзник был в {_num(a['nearest_ally_m'])} м "
                    f"(обычно — около 40 м).",
        "moments": [a["t"] - 20],
        "fix": "На старте посмотрите, куда едут союзники, и выберите фланг, где будет хотя бы "
               "ещё один-два танка.",
    }


def _rule_misses(f):
    rank = f["lobby"].get("accuracy")
    me = f["me"]
    if not rank or not me["shots"] or me["shots"] < tactics.MIN_SHOTS or not _bad(rank):
        return None
    missed = (f["shots"] or {}).get("missed") or []
    return {
        "code": "misses",
        "title": "Много промахов",
        "severity": "high" if rank["rank"] == rank["of"] else "medium",
        "cause": "Значительная часть выстрелов ушла мимо: стрельба до сведения, по движущимся "
                 "целям без упреждения или на предельной дистанции.",
        "evidence": f"Попаданий {me['hits']} из {me['shots']} ({_pct(rank['you'])}) — "
                    f"{rank['rank']}-е место из {rank['of']} (медиана {_pct(rank['median'])}).",
        "moments": missed[:4],
        "fix": "Стреляйте после полного сведения; по движущимся — с упреждением, по дальним "
               "целям — в центр силуэта, а не в уязвимые места.",
    }


def _rule_no_pen(f):
    rank = f["lobby"].get("pen_rate")
    me = f["me"]
    if not rank or not me["hits"] or me["hits"] < tactics.MIN_HITS or not _bad(rank):
        return None
    wasted = (f["shots"] or {}).get("no_damage") or []
    return {
        "code": "no_pen",
        "title": "Попадания без пробития",
        "severity": "high" if rank["rank"] == rank["of"] else "medium",
        "cause": "Снаряды попадали в крепкую броню или под углом — рикошеты и непробития "
                 "вместо урона.",
        "evidence": f"Пробитий {me['pens']} из {me['hits']} попаданий ({_pct(rank['you'])}) — "
                    f"{rank['rank']}-е место из {rank['of']} (медиана {_pct(rank['median'])}).",
        "moments": [t for t, _kind in wasted[:4]],
        "fix": "Целитесь в нижний бронелист, борта и стык башни; против крепкой брони заранее "
               "переключайтесь на бронебойный или кумулятивный снаряд.",
    }


def _rule_passive(f):
    rank = f["lobby"].get("damage")
    me = f["me"]
    if not rank or me["alive"] is not True or not me["max_hp"]:
        return None
    if not _bad(rank) or me["damage"] >= 0.5 * me["max_hp"]:
        return None
    shots = me["shots"] or 0
    return {
        "code": "passive",
        "title": "Выжили, но почти не повлияли на бой",
        "severity": "medium",
        "cause": "Танк дожил до конца, но почти не стрелял по противнику: прочность, которая "
                 "не превратилась в урон, команде не помогла.",
        "evidence": f"{_num(me['damage'])} урона за бой ({shots} "
                    f"{_plu(shots, 'выстрел', 'выстрела', 'выстрелов')}) — {rank['rank']}-е место "
                    f"из {rank['of']} при {_num(me['max_hp'])} HP прочности.",
        "moments": [],
        "fix": "Ищите позицию, откуда можно стрелять, не подставляясь, и подключайтесь к "
               "перестрелкам союзников раньше.",
    }


RULES = (_rule_early_death, _rule_cheap_death, _rule_isolated_death, _rule_focus,
         _rule_standing, _rule_alone_start, _rule_misses, _rule_no_pen, _rule_passive)


def _strengths(f):
    """Up to two results that genuinely beat the lobby: rank 1 or 2 AND better
    than the median -- ties share rank 1, so a metric nobody scored on would
    otherwise come out as "the best in the battle"."""
    me = f["me"]
    candidates = [
        ("damage", lambda: f"{_num(me['damage'])} урона", "по урону"),
        ("kills", lambda: f"{me['kills']} {_plu(me['kills'], 'фраг', 'фрага', 'фрагов')}",
         "по фрагам"),
        ("damage_net", lambda: f"размен прочности {_signed(f['lobby']['damage_net']['you'])}",
         "по разнице нанесённого и полученного урона"),
        ("accuracy", lambda: f"точность {_pct(f['lobby']['accuracy']['you'])}", "по точности"),
        ("pen_rate", lambda: f"пробиваемость {_pct(f['lobby']['pen_rate']['you'])}",
         "по доле пробитий"),
        ("assist", lambda: f"{_num(me['assist'])} урона по вашему засвету и гусеницам",
         "по помощи команде"),
        ("damage_blocked", lambda: f"{_num(f['lobby']['damage_blocked']['you'])} урона "
         "заблокировано бронёй", "по заблокированному урону"),
    ]
    out = []
    for key, phrase, what in candidates:
        rank = f["lobby"].get(key)
        if not rank or rank["rank"] > 2:
            continue
        beats = rank["you"] > rank["median"] if tactics.BETTER[key] else rank["you"] < rank["median"]
        if not beats:
            continue
        if key == "accuracy" and (me["shots"] or 0) < tactics.MIN_SHOTS:
            continue
        if key == "pen_rate" and (me["hits"] or 0) < tactics.MIN_HITS:
            continue
        text = phrase()
        place = "лучший результат" if rank["rank"] == 1 else "второй результат"
        out.append({"key": key, "point": text[0].upper() + text[1:], "place": place,
                    "evidence": f"{place} в бою {what} из {rank['of']}."})
        if len(out) == 2:
            break
    return out


def findings(facts):
    """Every problem the rules find in one battle, the worst first. The report
    shows three; the career counts them all."""
    found = []
    for rule in RULES:
        try:
            problem = rule(facts)
        except (KeyError, TypeError):
            continue  # a fact this battle does not have; the rule does not apply
        if problem:
            problem["moments"] = [round(max(0.0, t), 1) for t in problem["moments"]]
            found.append(problem)
    found.sort(key=lambda p: (SEVERITY_ORDER[p["severity"]], WEIGHT[p["code"]]), reverse=True)
    return found


def build_report(facts, found=None):
    """The report dict of one battle (see the module docstring); `found` is
    findings(facts) when the caller already has it."""
    if not facts:
        raise ValueError("no fact sheet: the replay does not say who recorded it")
    if found is None:
        found = findings(facts)
    top = found[:3]
    strengths = _strengths(facts)

    me = facts["me"]
    if top:
        lead = top[0]["title"][0].lower() + top[0]["title"][1:]
        if strengths:
            point = strengths[0]["point"]
            headline = f"Сильная сторона — {point[0].lower() + point[1:]}, но {lead}."
        else:
            headline = f"Главное в этом бою: {lead}."
    elif strengths:
        headline = (f"{strengths[0]['point']} ({strengths[0]['place']} в бою), и грубых "
                    "ошибок разбор не нашёл.")
    elif me["damage"] is not None:
        kills = me["kills"] or 0
        headline = (f"Грубых ошибок разбор не нашёл: {_num(me['damage'])} урона, {kills} "
                    f"{_plu(kills, 'фраг', 'фрага', 'фрагов')}.")
    else:
        headline = "Грубых ошибок разбор не нашёл."
    if not facts["has_results"]:
        headline += " Итогов боя в реплее нет, поэтому без сравнения с лобби."

    code = top[0]["code"] if top else None
    name, how, target = DRILLS.get(code, (
        "Держать уровень", "Слабых мест по этому бою не нашлось — загрузите следующие бои: "
        "разбор по многим боям покажет повторяющиеся ошибки.",
        "Ни одной ошибки из разбора в следующих боях."))
    return {"headline": headline, "strengths": strengths, "problems": top,
            "drill": {"name": name, "how": how, "target": target}}


def analyse_and_build(battle):
    """The fact sheet and the report of one battle dict; (None, None) when the
    recorder is unknown."""
    facts = tactics.analyse(battle)
    if facts is None:
        return None, None
    return facts, build_report(facts)
