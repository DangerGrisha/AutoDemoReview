"""tools/hud_icons.txt, which decides which weapons get a CS2 icon on the replay's HUD.

What a user would notice: a weapon staying a text label among icons, or the page asking
for a file the list never builds.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _listed():
    rows = {}
    for line in (REPO / "tools/hud_icons.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            name, icon = line.split()
            rows[name] = icon
    return rows


class HudIconsTest(unittest.TestCase):
    def test_every_tag_the_extractor_gives_has_an_icon(self):
        source = (REPO / "rust/extract/src/weapons.rs").read_text(encoding="utf-8")
        table = source[source.index("WEAPON_TAGS"):source.index("KNIFE_WORDS")]
        events = source[source.index("fn event_weapon_tag"):source.index("pub fn weapon_tag")]
        tags = set(re.findall(r'\("[^"]+", "(\w+)"\)', table)) | set(re.findall(r'=> "(\w+)"', events))
        self.assertGreater(len(tags), 30)
        self.assertEqual(tags - set(_listed()), set())

    def test_the_names_the_page_spells_for_emoji_tags_are_listed(self):
        for page in ("web/templates/replay3d.html", "rust/viewer/www/index.html"):
            source = (REPO / page).read_text(encoding="utf-8")
            match = re.search(r"const EMOJI_ICONS\s*=\s*\{([^}]*)\}", source)
            self.assertIsNotNone(match, page)
            names = set(re.findall(r":\s*'(\w+)'", match.group(1)))
            self.assertEqual(names, {"knife", "c4", "fire"}, page)
            self.assertEqual(names - set(_listed()), set(), page)


if __name__ == "__main__":
    unittest.main()
