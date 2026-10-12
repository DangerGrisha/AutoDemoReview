"""Static game data: tank and map names, tiers, classes, minimaps.

Everything comes from assets/ (generated from an installed client by
tools/wotb_extract_assets.py and committed), so the analyzer works offline and
never calls the Wargaming API. Anything the tables do not know -- a tank added
after the last extraction -- degrades to its id, never to an exception.
"""

import json
from functools import lru_cache
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"

# Order of the nation nibble inside a tank id (vehicles/common/nations.xml).
NATIONS = ("ussr", "germany", "usa", "china", "france", "uk", "japan", "other", "european")

MODE_NAMES = {0: {"ru": "Встречный бой", "en": "Encounter"},
              1: {"ru": "Превосходство", "en": "Supremacy"}}

CLASS_NAMES = {"LT": {"ru": "ЛТ", "en": "LT"},
               "MT": {"ru": "СТ", "en": "MT"},
               "HT": {"ru": "ТТ", "en": "HT"},
               "TD": {"ru": "ПТ-САУ", "en": "TD"},
               "SPG": {"ru": "САУ", "en": "SPG"}}

ROMAN = ("", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")


@lru_cache(maxsize=None)
def _table(name, section):
    path = ASSETS / name
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get(section, {})


def vehicle(tank_id, lang="ru"):
    """Display data for a tank id; `known` is False when the table lacks it."""
    entry = _table("vehicles.json", "vehicles").get(str(tank_id))
    if entry is None:
        nation = (tank_id >> 4) & 0xF if tank_id else None
        return {"tank_id": tank_id, "key": None, "name": f"#{tank_id}",
                "short": f"#{tank_id}", "tier": None, "type": None,
                "nation": NATIONS[nation] if nation is not None and nation < len(NATIONS) else None,
                "premium": False, "known": False}
    return {"tank_id": tank_id, "key": entry["key"],
            "name": entry.get(f"name_{lang}") or entry["key"],
            "short": entry.get(f"short_{lang}") or entry["key"],
            "tier": entry.get("tier"), "type": entry.get("type"),
            "nation": entry.get("nation"), "premium": bool(entry.get("premium")),
            "known": True}


def map_info(key=None, map_id=None, lang="ru"):
    """Display data for a map by its key ("canyon") or numeric id (27)."""
    maps = _table("maps.json", "maps")
    entry = maps.get(key) if key else None
    if entry is None and map_id is not None:
        entry = next((m for m in maps.values() if m.get("id") == map_id), None)
    if entry is None:
        return {"key": key, "id": map_id, "name": key or f"#{map_id}",
                "known": False, "has_minimap": False}
    return {"key": entry["key"], "id": entry.get("id"),
            "name": entry.get(f"name_{lang}") or entry["key"],
            "known": True, "has_minimap": minimap_path(entry["key"]) is not None}


def minimap_path(key):
    if not key:
        return None
    path = ASSETS / "minimaps" / f"{key}.webp"
    return path if path.is_file() else None


def mode_name(mode, lang="ru"):
    names = MODE_NAMES.get(mode)
    return names[lang] if names else None


def class_name(code, lang="ru"):
    names = CLASS_NAMES.get(code)
    return names[lang] if names else None


def tier_roman(tier):
    return ROMAN[tier] if isinstance(tier, int) and 0 < tier < len(ROMAN) else "?"
