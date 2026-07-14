//! Port of `src/demoreview/replay.py`'s WEAPON_TAGS / weapon_tag (feature/3d-replay).
//! `active_weapon_name` -> short display tag. Covers the full CS2 arsenal.

const WEAPON_TAGS: &[(&str, &str)] = &[
    // rifles
    ("AK-47", "AK"),
    ("M4A4", "M4"),
    ("M4A1-S", "M4S"),
    ("AUG", "AUG"),
    ("SG 553", "SG553"),
    ("FAMAS", "FAMAS"),
    ("Galil AR", "GALIL"),
    // snipers
    ("AWP", "AWP"),
    ("SSG 08", "SCOUT"),
    ("SCAR-20", "SCAR"),
    ("G3SG1", "G3"),
    // smgs
    ("MP9", "MP9"),
    ("MP7", "MP7"),
    ("MP5-SD", "MP5"),
    ("MAC-10", "MAC10"),
    ("UMP-45", "UMP"),
    ("P90", "P90"),
    ("PP-Bizon", "BIZON"),
    // pistols
    ("Glock-18", "GLOCK"),
    ("USP-S", "USP"),
    ("P2000", "P2000"),
    ("P250", "P250"),
    ("Five-SeveN", "FIVE7"),
    ("Tec-9", "TEC9"),
    ("CZ75-Auto", "CZ"),
    ("Dual Berettas", "DUALS"),
    ("Desert Eagle", "DEAG"),
    ("R8 Revolver", "R8"),
    // heavy
    ("Nova", "NOVA"),
    ("XM1014", "XM"),
    ("Sawed-Off", "SAWED"),
    ("MAG-7", "MAG7"),
    ("M249", "M249"),
    ("Negev", "NEGEV"),
    // grenades in hand
    ("High Explosive Grenade", "HE"),
    ("Flashbang", "FLASH"),
    ("Smoke Grenade", "SMOKE"),
    ("Molotov", "MOLLY"),
    ("Incendiary Grenade", "INC"),
    ("Decoy Grenade", "DECOY"),
];

const KNIFE_WORDS: &[&str] = &[
    "karambit", "daggers", "talon", "ursus", "navaja", "stiletto", "skeleton", "huntsman",
    "falchion", "bowie", "shadow", "paracord", "nomad", "survival", "gut", "classic",
];

/// Short display tag for a held weapon, or None when there's nothing to show.
pub fn weapon_tag(name: Option<&str>) -> Option<String> {
    let name = name?;
    if name.is_empty() {
        return None;
    }
    if let Some((_, tag)) = WEAPON_TAGS.iter().find(|(k, _)| *k == name) {
        return Some((*tag).to_string());
    }
    let n = name.to_lowercase();
    if n.contains("c4") {
        return Some("💣".to_string());
    }
    if n.contains("knife")
        || n.contains("bayonet")
        || n == "knife"
        || n == "knife_t"
        || KNIFE_WORDS.iter().any(|w| n.contains(w))
    {
        return Some("🔪".to_string());
    }
    // unknown: show the raw name, uppercased, first 6 CHARS (not bytes)
    Some(name.to_uppercase().chars().take(6).collect())
}
