"""The real CS2 sounds (tools/prepare_sounds.py), on small synthetic dumps.

What a user would notice: a gun with the wrong sound or volume, a sound that silently
never gets built, or a looping bed coming back.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import prepare_sounds as ps  # noqa: E402

DUMP = """--- Data for block "RED2" ---
{
    Weapon_Fake.Single =
    {
        vsnd_files = "sounds/weapons/wrong/red2_block.vsnd"
    }
}
--- Data for block "DATA" ---
{
    Base.Footstep =
    {
        volume = 0.4
        pitch = 0.9
        pitch_random_min = -0.02
        pitch_random_max = 0.02
        instance_limit = 0.0
        distance_volume_mapping_curve =
        [
            [ 49.5, 0.45, 0.01, 0.01, 3.0, 1.0, ],
            [ 1100.0, 0.0, -0.0004, -0.0004, 2.0, 3.0, ],
        ]
    }
    Weapon_Muted.Single =
    {
        base = "Base.Footstep"
        volume = 0.0
    }
    CT_Concrete.StepLeft =
    {
        base = "Base.Footstep"
        vsnd_files =
        [
            "sounds/player/footsteps/new/concrete_ct_01.vsnd",
            "sounds/player/footsteps/new/concrete_ct_02.vsnd",
        ]
    }
    Weapon_AK47.Single =
    {
        type = "csgo_mega"
        volume = 1.1
        instance_limit = 1.0
        vsnd_files =
        [
            "sounds/weapons/ak47/ak47_02.vsnd",
            "sounds/weapons/ak47/ak47_01.vsnd",
        ]
        volume_falloff_curve = { volume = 0.1 }
        distance_volume_mapping_curve =
        [
            [ 25.0, 0.8, 0.04, 0.04, 1.0, 1.0, ],
            [ 30.0, 1.0, 0.001, 0.001, 1.0, 1.0, ],
            [ 2500.0, 0.014706, -0.0001, -0.0001, 2.0, 3.0, ],
        ]
        distance_unfiltered_stereo_mapping_curve =
        [
            [ 35.0, 1.0, 0.0, 0.0, 0.0, 0.0, ],
        ]
    }
    Weapon_NoFiles.Single =
    {
        volume = 1.0
    }
}
"""


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.events = ps.parse_sound_events(DUMP)

    def test_files_keep_the_events_own_order_and_unused_files_stay_out(self):
        self.assertEqual(self.events["Weapon_AK47.Single"]["files"],
                         ["sounds/weapons/ak47/ak47_02.vsnd", "sounds/weapons/ak47/ak47_01.vsnd"])

    def test_the_first_volume_is_the_events_own_and_not_a_curves(self):
        self.assertEqual(self.events["Weapon_AK47.Single"]["volume"], 1.1)

    def test_a_block_before_data_does_not_define_events(self):
        self.assertNotIn("Weapon_Fake.Single", self.events)

    def test_a_base_fills_in_what_the_event_leaves_unset(self):
        step = ps.resolved(self.events, "CT_Concrete.StepLeft")
        self.assertEqual((step["volume"], step["pitch"]), (0.4, 0.9))
        self.assertEqual(len(step["files"]), 2)

    def test_a_missing_pitch_defaults_to_normal_speed(self):
        self.assertEqual(ps.resolved(self.events, "Weapon_AK47.Single")["pitch"], 1.0)

    def test_an_unknown_event_resolves_to_nothing(self):
        self.assertIsNone(ps.resolved(self.events, "Weapon_Nope.Single"))

    def test_a_base_cycle_does_not_hang(self):
        empty = {"files": [], "volume": None, "pitch": None, "curve": [],
                 "pitch_min": None, "pitch_max": None, "limit": None}
        events = {"A": dict(empty, base="B"), "B": dict(empty, base="A")}
        self.assertEqual(ps.resolved(events, "A")["volume"], 1.0)

    def test_a_silent_event_stays_silent_whatever_its_base_says(self):
        self.assertEqual(ps.resolved(self.events, "Weapon_Muted.Single")["volume"], 0.0)

    def test_the_distance_curve_is_the_volume_one_and_keeps_only_distance_and_volume(self):
        self.assertEqual(self.events["Weapon_AK47.Single"]["curve"],
                         [[25.0, 0.8], [30.0, 1.0], [2500.0, 0.014706]])

    def test_an_event_inherits_the_curve_of_its_base(self):
        self.assertEqual(ps.resolved(self.events, "CT_Concrete.StepLeft")["curve"], [[49.5, 0.45], [1100.0, 0.0]])

    def test_an_entry_says_only_what_changes_something(self):
        ak = ps.index_entry(ps.resolved(self.events, "Weapon_AK47.Single"), ["a.ogg"])
        self.assertEqual(ak["n"], 1)
        self.assertNotIn("r", ak)
        step = ps.index_entry(ps.resolved(self.events, "CT_Concrete.StepLeft"), ["a.ogg"])
        self.assertEqual(step["r"], [-0.02, 0.02])
        self.assertNotIn("n", step)   # 0 in CS2 means no limit


class ListTest(unittest.TestCase):
    def setUp(self):
        self.rows = ps.parse_event_list((REPO / "tools/sound_events.txt").read_text(encoding="utf-8"))
        self.keys = [key for key, _ in self.rows]

    def test_comments_and_blank_lines_are_skipped(self):
        self.assertEqual(ps.parse_event_list("# no\n\na B\n"), [("a", "B")])

    def test_each_key_appears_once(self):
        self.assertEqual(len(self.keys), len(set(self.keys)))

    def test_every_gun_the_extractor_tags_has_a_shot(self):
        source = (REPO / "rust/extract/src/weapons.rs").read_text(encoding="utf-8")
        table = source[source.index("WEAPON_TAGS"):source.index("// grenades in hand")]
        tags = re.findall(r'\("[^"]+", "(\w+)"\)', table)
        self.assertGreater(len(tags), 30)
        for tag in tags:
            self.assertIn(f"shot:{tag}", self.keys)

    def test_the_constant_beds_the_user_hated_stay_out(self):
        for _, event in self.rows:
            self.assertNotIn("loop", event.lower())

    def test_every_key_sfx_js_asks_for_by_name_is_listed(self):
        source = (REPO / "web/static/replay3d/sfx.js").read_text(encoding="utf-8")
        asked = set(re.findall(r"""sample\(\s*'([\w:]+)'\s*,""", source))
        self.assertGreater(len(asked), 8)
        self.assertEqual(asked - set(self.keys), set())


class OutputNameTest(unittest.TestCase):
    def test_flat_and_unique(self):
        self.assertEqual(ps.output_name("sounds/weapons/ak47/ak47_01.vsnd"), "weapons_ak47_ak47_01.ogg")
        self.assertNotEqual(ps.output_name("sounds/a/b_c.vsnd"), ps.output_name("sounds/a_b/d.vsnd"))

    def test_url_unsafe_characters_go(self):
        self.assertEqual(ps.output_name("sounds/weapons/ak47/ak47 #1.vsnd"), "weapons_ak47_ak47__1.ogg")


class BuildIndexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.files = self.root / "files"
        self.out = self.root / "out"
        self.out.mkdir()
        self.encoded = []
        self.events = ps.parse_sound_events(DUMP)

    def _source(self, relative):
        path = self.files / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    def _encode(self, source, out):
        self.encoded.append(source.name)
        Path(out).write_bytes(b"ogg")

    def test_a_built_entry_has_its_files_volume_and_pitch(self):
        self._source("sounds/weapons/ak47/ak47_01.wav")
        self._source("sounds/weapons/ak47/ak47_02.mp3")
        index, warnings = ps.build_index(self.events, [("shot:AK", "Weapon_AK47.Single")], self.files, self.out, self._encode)
        entry = index["shot:AK"]
        self.assertEqual(entry["f"], ["weapons_ak47_ak47_02.ogg", "weapons_ak47_ak47_01.ogg"])
        self.assertEqual((entry["v"], entry["p"]), (1.1, 1.0))
        self.assertEqual(warnings, [])

    def test_two_keys_sharing_a_file_encode_it_once(self):
        self._source("sounds/weapons/ak47/ak47_01.wav")
        self._source("sounds/weapons/ak47/ak47_02.wav")
        wanted = [("a", "Weapon_AK47.Single"), ("b", "Weapon_AK47.Single")]
        index, _ = ps.build_index(self.events, wanted, self.files, self.out, self._encode)
        self.assertEqual(len(self.encoded), 2)
        self.assertEqual(index["a"]["f"], index["b"]["f"])

    def test_a_missing_file_warns_and_the_rest_of_the_event_survives(self):
        self._source("sounds/weapons/ak47/ak47_01.wav")
        index, warnings = ps.build_index(self.events, [("shot:AK", "Weapon_AK47.Single")], self.files, self.out, self._encode)
        self.assertEqual(index["shot:AK"]["f"], ["weapons_ak47_ak47_01.ogg"])
        self.assertTrue(any("ak47_02" in w for w in warnings))

    def test_an_unknown_event_or_one_without_files_warns_and_is_left_for_the_synth(self):
        wanted = [("a", "Weapon_Nope.Single"), ("b", "Weapon_NoFiles.Single")]
        index, warnings = ps.build_index(self.events, wanted, self.files, self.out, self._encode)
        self.assertEqual(index, {})
        self.assertEqual(len(warnings), 2)

    def test_a_source_ffmpeg_cannot_read_warns_and_does_not_stop_the_build(self):
        self._source("sounds/weapons/ak47/ak47_01.wav")
        self._source("sounds/weapons/ak47/ak47_02.wav")

        def fail_on_one(source, out):
            if source.name == "ak47_02.wav":
                raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"bad")
            Path(out).write_bytes(b"ogg")

        index, warnings = ps.build_index(self.events, [("shot:AK", "Weapon_AK47.Single")], self.files, self.out, fail_on_one)
        self.assertEqual(index["shot:AK"]["f"], ["weapons_ak47_ak47_01.ogg"])
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
