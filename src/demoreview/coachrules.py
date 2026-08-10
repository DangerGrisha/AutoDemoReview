"""Layer 7b -- the free coach: a rule engine over the tactical fact sheet.

The paid coach sent `tactics.analyse` output to an LLM and paid it to pick the
important findings and phrase them. Everything it was paid for is a judgement
call over data that is already measured, so most of it can be encoded directly:
each rule knows one failure mode, when it fires, how badly, and what to say.

It emits the SAME report dict the LLM produced, so caching, markdown rendering
and the round deep-links into the 3D replay are unchanged.

What a rule engine cannot do, and does not pretend to: notice a pattern nobody
wrote a rule for, or fuse three unrelated metrics into a single root cause. It
compensates by never inventing anything and never costing anything.

Output is Russian only -- these are hand-written strings, not translations.
"""

from . import tactics

# Ranks are out of the ten players in that match; 1 is best. A player in the
# bottom three on a metric has a real problem with it at their own level.
BAD_RANK = 8
POOR_RANK = 7

# Inherent weight of each failure mode, independent of how badly it scored.
# Dying first every round costs more than a wide spray, even at equal rank.
WEIGHT = {
    "early_death": 100,
    "hotspot": 95,
    "opening_duels": 85,
    "teamflash": 80,
    "isolation": 75,
    "no_trade": 60,
    "spray": 50,
    "no_util": 45,
    "smoke_deaths": 30,
}


def _plu(n, one, few, many):
    """Russian noun agreement for a count (1 раунд / 2 раунда / 5 раундов)."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _rounds_word(n):
    return _plu(n, "раунд", "раунда", "раундов")


def _deaths_word(n):
    return _plu(n, "смерть", "смерти", "смертей")


def _kills_word(n):
    return _plu(n, "килл", "килла", "киллов")


def _list_rounds(rounds):
    """'раунд 4' / 'раунды 4, 6, 8' -- nominative, for listing rather than counting."""
    word = "раунд" if len(rounds) == 1 else "раунды"
    return f"{word} " + ", ".join(str(n) for n in rounds)


def _m(facts, key):
    """The vs_lobby entry for `key`, or None when it wasn't ranked."""
    return (facts.get("vs_lobby") or {}).get(key)


def _severity(rank, of):
    """Bottom two -> high, bottom four -> medium, else low."""
    if rank >= of - 1:
        return "high"
    if rank >= of - 3:
        return "medium"
    return "low"


def _worst_rounds(facts, pick, limit=5):
    """Round numbers matching `pick`, worst first by the round's own sort key."""
    rows = [r for r in facts.get("rounds", []) if pick(r)]
    return [r["n"] for r in rows[:limit]]


# --------------------------------------------------------------------------- #
# rules -- each returns a problem dict or None
# --------------------------------------------------------------------------- #

def _rule_early_death(facts, agg):
    m = _m(facts, "avg_death_time_s")
    if not m or m["rank"] < POOR_RANK:
        return None
    deaths = sorted((r for r in facts["rounds"] if r.get("death_s")),
                    key=lambda r: r["death_s"])
    gap = round(m["lobby_median"] - m["you"], 1)
    return {
        "code": "early_death",
        "title": "Ты умираешь раньше всех в лобби",
        "severity": _severity(m["rank"], m["of"]),
        "cause": "Ты берёшь первый контакт раунда до того, как команда вышла "
                 "вместе с тобой и до того, как отработала утилита. Пока ты "
                 "открываешь размен собой, тиммейты ещё не в позиции, чтобы "
                 "тебя добрать.",
        "evidence": f"Средняя секунда смерти {m['you']} с против "
                    f"{m['lobby_median']} с по лобби — {m['rank']} место из "
                    f"{m['of']}, то есть на {gap} с раньше остальных. Самые "
                    f"ранние: " + ", ".join(
                        f"раунд {r['n']} на {r['death_s']} с" for r in deaths[:3]) + ".",
        "rounds": [r["n"] for r in deaths[:5]],
        "fix": "Первые 20 секунд раунда играй вторым номером: контакт открывает "
               "утилита или тиммейт, ты добираешь. Ориентир на следующий матч — "
               "не умирать раньше 30-й секунды.",
    }


def _zone(facts):
    """The callout of the death hotspot, if the map's zones are known."""
    return (facts.get("death_hotspot") or {}).get("zone")


def _rule_hotspot(facts, agg):
    h = facts.get("death_hotspot")
    if not h or h["deaths"] < 3:
        return None
    rounds = h["rounds"]
    lost = [r["n"] for r in facts["rounds"]
            if r["n"] in rounds and r.get("won") is False]
    lost_note = (f" Из них проиграно {len(lost)} — "
                 f"{', '.join(str(n) for n in lost)}." if lost else "")
    # A callout is what the player thinks in; coordinates are what is left when
    # the map has no zone data (an unconverted or community map).
    zone = h.get("zone")
    where = f"на {zone}" if zone else "в одной точке карты"
    place = (f"{where} (координаты {h['x']}, {h['y']})" if not zone else
             f"{where} (радиус около 450 юнитов вокруг {h['x']}, {h['y']})")
    return {
        "code": "hotspot",
        "title": f"Ты раз за разом умираешь на {zone}" if zone else
                 "Ты умираешь в одной и той же точке",
        "severity": "high" if h["deaths"] >= 4 else "medium",
        "cause": "Ты возвращаешься на одну и ту же позицию раунд за раундом. "
                 "К середине матча соперник знает, где ты стоишь, и заходит "
                 "туда с утилитой и заранее наведённым прицелом.",
        "evidence": f"{h['deaths']} {_deaths_word(h['deaths'])} {place} — "
                    f"{_list_rounds(rounds)}.{lost_note}",
        "rounds": rounds,
        "fix": (f"Держи {zone} не чаще чем через раунд. " if zone else
                "Держи эту позицию не чаще чем через раунд. ") +
               "Если всё же встал туда второй раз подряд — меняй угол подхода "
               "и время выхода, чтобы тебя не ждали на той же секунде.",
    }


def _rule_teamflash(facts, agg):
    tb = _m(facts, "teammates_blinded")
    bad = _m(facts, "bad_flash_pct")
    you = facts["you"]
    if not tb or (tb["rank"] < POOR_RANK and not (bad and bad["you"] >= 45)):
        return None
    rounds = _worst_rounds(
        facts, lambda r: (r.get("util") or []).count("flash") >= 2
        and r.get("won") is False)
    share = f" Это {bad['you']}% всего времени слепоты от твоих флешек." if bad else ""
    return {
        "code": "teamflash",
        "title": "Твои флешки слепят команду не меньше, чем противника",
        "severity": _severity(tb["rank"], tb["of"]),
        "cause": "Флешки летят по прямой вместе с движением команды, а не за "
                 "угол. Тиммейт, который идёт впереди тебя, попадает во "
                 "вспышку раньше противника.",
        "evidence": f"Ослеплено своих: {tb['you']} при медиане {tb['lobby_median']} "
                    f"по лобби ({tb['rank']} место из {tb['of']}). Суммарная "
                    f"слепота своих {you['team_blind_s']} с против "
                    f"{you['enemy_blind_s']} с у противника.{share} "
                    f"Флеш-ассистов при этом {you['flash_conversion_pct'] or 0}% "
                    f"от {you['flashes_thrown']} брошенных флешек.",
        "rounds": rounds,
        "fix": "Перед броском проверяй, кто в 500 юнитах впереди тебя. Кидай "
               "только из-за укрытия так, чтобы вспышка сработала за углом, вне "
               "твоей линии взгляда и линии тиммейта. Лучше 1-2 флешки за раунд "
               "под конкретный выход, чем пять на бегу.",
    }


def _rule_opening_duels(facts, agg):
    m = _m(facts, "opening_win_pct")
    you = facts["you"]
    if not m or m["rank"] < POOR_RANK or you["opening_duels"] < 4:
        return None
    lost = you["opening_duels"] - round(you["opening_duels"] * (m["you"] or 0) / 100)
    return {
        "code": "opening_duels",
        "title": "Ты берёшь много первых дуэлей и проигрываешь их",
        "severity": _severity(m["rank"], m["of"]),
        "cause": "Ты выходишь в первый контакт чаще всех в команде, но в "
                 "невыгодных условиях — без флешки, без поддержки и на дистанции, "
                 "где противник видит тебя первым.",
        "evidence": f"{you['opening_duels']} первых дуэлей за матч, побед "
                    f"{m['you']}% против {m['lobby_median']}% по лобби "
                    f"({m['rank']} место из {m['of']}). Это {lost} проигранных "
                    f"первых дуэлей.",
        "rounds": _worst_rounds(facts, lambda r: r.get("opening") == "lost"),
        "fix": "Не бери первую дуэль без флешки или без тиммейта в 500 юнитах "
               "за спиной. Если утилиты нет — отдай контакт и играй вторым.",
    }


def _rule_isolation(facts, agg):
    """Median spacing can look fine while a few rounds are catastrophic."""
    far = [r for r in facts["rounds"]
           if (r.get("nearest_mate_u") or 0) >= 1200]
    med = _m(facts, "median_isolation_u")
    median_bad = med and med["rank"] >= BAD_RANK
    if len(far) < 3 and not median_bad:
        return None
    far.sort(key=lambda r: -r["nearest_mate_u"])
    detail = ", ".join(
        f"раунд {r['n']} — {r['nearest_mate_u']} юнитов"
        + (f" ({r['zone']})" if r.get("zone") else "")
        for r in far[:3])
    if median_bad:
        head = (f"Медианная дистанция до ближайшего живого тиммейта в момент "
                f"смерти {med['you']} юнитов против {med['lobby_median']} по "
                f"лобби ({med['rank']} место из {med['of']}). ")
    else:
        head = (f"В большинстве раундов ты стоишь близко к команде, но есть "
                f"{len(far)} {_rounds_word(len(far))}, где ты один. ")
    return {
        "code": "isolation",
        "title": "Ты умираешь там, где тебя некому разменять",
        "severity": "high" if median_bad else "medium",
        "cause": "Ты уходишь на позицию, до которой ближайший тиммейт не "
                 "успевает. Такая смерть не только бесплатна для соперника — "
                 "она ещё и отдаёт ему numbers на остаток раунда.",
        "evidence": head + f"Худшие: {detail}. Не разменяно "
                           f"{facts['you']['untraded_death_pct']}% твоих смертей.",
        "rounds": [r["n"] for r in far[:5]],
        "fix": "Правило дистанции: если ближайший тиммейт дальше 800 юнитов — "
               "не занимай позицию, а подтягивайся к нему. Позиция, которую "
               "некому перестрелять после твоей смерти, не стоит информации.",
    }


def _rule_no_trade(facts, agg):
    m = _m(facts, "trade_conversion_pct")
    you = facts["you"]
    if not m or m["rank"] < POOR_RANK or you["trade_chances"] < 8:
        return None
    missed = you["trade_chances"] - you["trades_made"]
    return {
        "code": "no_trade",
        "title": "Ты не добираешь за упавшими тиммейтами",
        "severity": _severity(m["rank"], m["of"]),
        "cause": "Ты был рядом и живой в момент, когда тиммейт умирал, но не "
                 "перезашёл на убийцу в окно размена. Обычно это значит, что ты "
                 "стоишь близко физически, но смотришь не туда.",
        "evidence": f"{you['trade_chances']} ситуаций, где тиммейт умирал в 800 "
                    f"юнитах от тебя, пока ты был жив. Разменял ты {you['trades_made']}, "
                    f"то есть {m['you']}% против {m['lobby_median']}% по лобби "
                    f"({m['rank']} место из {m['of']}). Упущено {missed}.",
        "rounds": [r["n"] for r in sorted(
            (r for r in facts["rounds"] if r.get("missed_trades")),
            key=lambda r: -r["missed_trades"])[:5]],
        "fix": "Стой не рядом с тиммейтом, а в кроссфайре с ним: смотри тот же "
               "угол под другой линией. Когда он падает — у тебя уже наведён "
               "прицел туда, откуда стреляли, и размен занимает полсекунды.",
    }


def _rule_spray(facts, agg):
    m = _m(facts, "shots_per_kill")
    if not m or m["rank"] < POOR_RANK:
        return None
    return {
        "code": "spray",
        "title": "Слишком много патронов на килл",
        "severity": _severity(m["rank"], m["of"]),
        "cause": "Ты доводишь спрей вместо того, чтобы отпустить кнопку и "
                 "перезайти. На средней дистанции очередь уходит вверх, и "
                 "добивает уже противник.",
        "evidence": f"{m['you']} выстрелов на килл против {m['lobby_median']} по "
                    f"лобби ({m['rank']} место из {m['of']}) при "
                    f"{facts['you']['hs_pct']}% в голову. Медианная дистанция до "
                    f"противника в момент твоей смерти — "
                    f"{facts['you']['median_enemy_dist_at_death_u']} юнитов.",
        # Match-wide metric: there is no round that demonstrates it, so cite none
        # rather than decorate the point with unrelated round numbers.
        "rounds": [],
        "fix": "На дистанции больше 500 юнитов — бёрст по 3-5 патронов с "
               "паузой на возврат прицела. Каждое утро 10 минут в aim_botz на "
               "тап по головам, без спрея вообще.",
    }


def _rule_no_util(facts, agg):
    m = _m(facts, "rounds_died_with_no_util_thrown")
    if not m or m["rank"] < BAD_RANK or m["you"] < 3:
        return None
    return {
        "code": "no_util",
        "title": "Ты умираешь с невыброшенной утилитой",
        "severity": "medium",
        "cause": "Гранаты остаются в инвентаре до самой смерти. Купленная и не "
                 "брошенная утилита — это деньги, потраченные впустую, и "
                 "информация, которую команда не получила.",
        "evidence": f"{m['you']} {_rounds_word(m['you'])}, где ты умер, не "
                    f"бросив ни одной гранаты, при медиане {m['lobby_median']} "
                    f"по лобби ({m['rank']} место из {m['of']}).",
        "rounds": _worst_rounds(facts, lambda r: r.get("death_s") and not r.get("util")),
        "fix": "Бросай смок или молотов на выходе, а не «когда понадобится». "
               "Если раунд идёт к концу и гранаты ещё в инвентаре — выкидывай "
               "их в проходы, где ждёшь заход.",
    }


def _rule_smoke_deaths(facts, agg):
    n = facts["you"]["deaths_through_smoke"]
    m = _m(facts, "deaths_through_smoke")
    if n < 3 or not m or m["rank"] < POOR_RANK:
        return None
    return {
        "code": "smoke_deaths",
        "title": "Тебя простреливают через смоки",
        "severity": "low",
        "cause": "Ты держишь позицию на прямой линии от свежего смока. "
                 "Противник кидает наугад в стандартную точку и попадает.",
        "evidence": f"{n} {_deaths_word(n)} через дым ({m['rank']} место из "
                    f"{m['of']}, медиана по лобби {m['lobby_median']}).",
        "rounds": [],
        "fix": "Как только рядом встал смок — сойди с линии, которую он "
               "перекрывает. Стандартные точки простреливают вслепую именно "
               "потому, что там стоят по привычке.",
    }


RULES = (_rule_early_death, _rule_hotspot, _rule_teamflash, _rule_opening_duels,
         _rule_isolation, _rule_no_trade, _rule_spray, _rule_no_util,
         _rule_smoke_deaths)

# One drill per failure mode, so the report ends on something to actually do.
DRILLS = {
    "early_death": ("Дисциплина тайминга", "10 раундов в дедматче/премьере с "
                    "одним правилом: до 20-й секунды раунда ты не открываешь "
                    "первый контакт ни разу. Считай вслух секунды до первого "
                    "выстрела по тебе.", "Средняя секунда смерти выше 35 с."),
    "hotspot": ("Смена позиции", "Выпиши три позиции на этой карте под свою "
                "роль и играй их строго по очереди, раунд за раундом. Ни одну "
                "не занимай дважды подряд.",
                "Ни одной точки с тремя и более смертями за матч."),
    "teamflash": ("Pop-flash из-за угла", "15 минут офлайн с ботами: по 5 "
                  "бросков на каждый стандартный выход. Бросок из-за укрытия, "
                  "вспышка срабатывает за углом, ты в этот момент спиной к ней, "
                  "заход через полсекунды.",
                  "Ослеплённых своих не больше 5 за матч и минимум 3 флеш-ассиста."),
    "opening_duels": ("Вход по утилите", "Отработай связку «флешка — заход» на "
                      "трёх выходах карты. Правило в матче: нет флешки — нет "
                      "первого контакта.",
                      "Побед в первых дуэлях выше 45%."),
    "isolation": ("Дистанция 800", "Весь матч держи ближайшего тиммейта в поле "
                  "800 юнитов. Как только оказался дальше — возвращайся, даже "
                  "если позиция кажется выгодной.",
                  "Ни одной смерти с тиммейтом дальше 1200 юнитов."),
    "no_trade": ("Кроссфайр вместо соседства", "Вставай так, чтобы вы с "
                 "тиммейтом смотрели один угол с разных линий. После каждой "
                 "его смерти сразу перезаходи на убийцу.",
                 "Конверсия разменов выше 35%."),
    "spray": ("Тап и бёрст", "10 минут aim_botz: только одиночные по головам, "
              "затем 10 минут бёрстом по 3 патрона с полным возвратом прицела.",
              "Выстрелов на килл ниже 18."),
    "no_util": ("Пустой инвентарь", "Правило на матч: к 40-й секунде раунда у "
                "тебя не должно остаться смока и молотова.",
                "Не больше одного раунда со смертью и невыброшенной утилитой."),
    "smoke_deaths": ("Сход с линии", "Отработай на карте три позиции, куда "
                     "уходишь сразу после появления смока рядом.",
                     "Ноль смертей через дым."),
}


def _strengths(facts):
    """Up to two genuine top-of-lobby results, phrased once and without padding."""
    you = facts["you"]
    candidates = [
        ("kills", f"{you['kills']} {_kills_word(you['kills'])} за матч", "по киллам"),
        ("deaths", f"всего {you['deaths']} смертей", "по живучести"),
        ("hs_pct", f"{you['hs_pct']}% в голову", "по проценту в голову"),
        ("median_isolation_u",
         f"медианная дистанция до тиммейта в момент смерти {you['median_isolation_u']} юнитов",
         "по игре в связке"),
        ("trade_conversion_pct",
         f"конверсия разменов {you['trade_conversion_pct']}%", "по разменам"),
        ("flash_conversion_pct",
         f"конверсия флешек {you['flash_conversion_pct']}%", "по флешкам"),
        ("untraded_death_pct",
         f"неразменянных смертей {you['untraded_death_pct']}%", "по разменянности смертей"),
    ]
    out = []
    for key, phrase, what in candidates:
        m = _m(facts, key)
        if not m or m["rank"] > 2:
            continue
        # Rank alone is not enough: ties all share rank 1, so a metric nobody in
        # the lobby scored on (an old artifact with no headshot flags, say)
        # would come out as "best in the server at 0% headshots". A strength has
        # to actually beat the field, not merely match it.
        direction = tactics.BETTER.get(key)
        beats_lobby = (m["you"] < m["lobby_median"] if direction == "low"
                       else m["you"] > m["lobby_median"])
        if not beats_lobby:
            continue
        place = "лучший результат" if m["rank"] == 1 else "второй результат"
        out.append({
            "point": phrase[0].upper() + phrase[1:],
            "evidence": f"{place} в лобби {what} (медиана {m['lobby_median']}).",
        })
        if len(out) == 2:
            break
    return out


def _merge_early_hotspot(found, facts):
    """Fold "dies early" and "dies in the same spot" into one finding.

    When the same rounds trigger both, they are one habit seen from two angles.
    Reporting them separately burns two of the three slots on one problem and
    pushes a genuinely different failure mode off the report.
    """
    early = next((p for p in found if p["code"] == "early_death"), None)
    hot = next((p for p in found if p["code"] == "hotspot"), None)
    if not (early and hot):
        return found
    shared = set(early["rounds"]) & set(hot["rounds"])
    if len(shared) < 2:
        return found

    zone = _zone(facts)
    merged = dict(early)
    merged["title"] = (f"Ты умираешь рано и всегда на {zone}" if zone else
                       "Ты умираешь рано и в одной и той же точке")
    merged["severity"] = "high"
    merged["cause"] = (
        f"Ты каждый раунд возвращаешься на {zone or 'одну позицию'} и берёшь "
        "оттуда первый контакт до того, как вышла утилита. Соперник читает и "
        "точку, и время, поэтому к середине матча тебя там просто ждут.")
    merged["evidence"] = early["evidence"] + " " + hot["evidence"]
    merged["rounds"] = sorted(set(early["rounds"]) | set(hot["rounds"]))
    merged["fix"] = (
        f"Первые 20 секунд раунда — никакого сухого пика "
        f"{'с ' + zone if zone else 'с этой позиции'}: либо открывай точку "
        "утилитой и заходи вторым номером, либо меняй её каждый второй раунд. "
        + early["fix"].split(". ", 1)[-1])
    return [p for p in found if p["code"] not in ("early_death", "hotspot")] + [merged]


def build_report(facts, agg=None):
    """Build the same report dict the LLM produced, with no API call.

    `facts` is `tactics.analyse` output. `agg` (the Layer 4/5 summary) is
    accepted for signature parity and future rules; nothing needs it yet.
    """
    if not facts:
        raise ValueError("no tactical fact sheet -- the free coach needs 3D data")

    found = []
    for rule in RULES:
        try:
            problem = rule(facts, agg)
        except (KeyError, TypeError):
            # A missing metric means that dimension wasn't measurable for this
            # match; skip the rule rather than fail the whole report.
            continue
        if problem:
            found.append(problem)

    found = _merge_early_hotspot(found, facts)
    order = {"high": 2, "medium": 1, "low": 0}
    found.sort(key=lambda p: (order[p["severity"]], WEIGHT.get(p["code"], 0)),
               reverse=True)
    top = found[:3]

    strengths = _strengths(facts)
    you = facts["you"]
    if top:
        lead = top[0]["title"][0].lower() + top[0]["title"][1:]
        if strengths:
            headline = (f"{strengths[0]['point']} — по перестрелкам ты в порядке, "
                        f"но {lead}, и это стоит команде раундов.")
        else:
            headline = (f"Главное за матч: {lead}. "
                        f"{you['kills']} {_kills_word(you['kills'])} при "
                        f"{you['deaths']} {_deaths_word(you['deaths'])} "
                        f"за {facts['rounds_analysed']} "
                        f"{_rounds_word(facts['rounds_analysed'])}.")
    else:
        headline = (f"Явных провалов по метрикам нет: {you['kills']} "
                    f"{_kills_word(you['kills'])} при {you['deaths']} "
                    f"{_deaths_word(you['deaths'])}, ничего в нижней трети лобби.")

    drill_code = top[0]["code"] if top else None
    name, how, target = DRILLS.get(
        drill_code,
        ("Поддерживать уровень", "Слабых мест по метрикам этого матча нет — "
         "разбери следующий матч и сравни ранги.", "Не опускаться ниже 5 места "
         "в лобби ни по одной метрике."))

    return {
        "headline": headline,
        "strengths": strengths,
        # `code` is dropped: it is internal routing, not something to read.
        "problems": [{k: v for k, v in p.items() if k != "code"} for p in top],
        "drill": {"name": name, "how": how, "target": target},
    }


def analyse_and_build(artifacts_dir, ref_sid, agg=None):
    """Convenience seam: fact sheet + report in one call."""
    facts = tactics.analyse(artifacts_dir, ref_sid)
    if facts is None:
        return None, None
    return build_report(facts, agg), facts
