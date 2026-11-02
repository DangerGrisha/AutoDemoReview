"""The real CS2 weapon models (tools/prepare_weapon.py), on tiny synthetic exports.

What a user would notice: a gun floating away from the player's hands or pointing the
wrong way, a gun lit from the wrong side, tracers leaving from nowhere, or the viewer
quietly hiding the model because of its material name.
"""

import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
    from PIL import Image
except ImportError as exc:  # the analysis suite must still run without the tool stack
    raise unittest.SkipTest(f"needs the tool stack: {exc}")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import prepare_weapon as pw  # noqa: E402

M = 0.0254


def _dump(bones, attachments=()):
    """Text in the shape of `Source2Viewer-CLI -a`, repeated once as a real model does."""
    out = []
    for name, parent, t in bones:
        out.append(f'''{{
            m_boneName = "{name}"
            m_parentName = "{parent}"
            m_invBindPose =
            [
                1.0, -0.0, 0.0, {t[0]},
                0.0, 1.0, -0.0, {t[1]},
                0.0, 0.0, 1.0, {t[2]},
            ]
        }},''')
    for name, bone, offset in attachments:
        out.append(f'''key = "{name}"
            value =
            {{
                m_name = "{name}"
                m_influenceNames =
                [
                    "{bone}",
                    "",
                    "",
                ]
                m_vInfluenceRotations =
                [
                    [ 0.0, 0.0, 0.0, 1.0 ],
                ]
                m_vInfluenceOffsets =
                [
                    [ {offset[0]}, {offset[1]}, {offset[2]} ],
                    [ 0.0, 0.0, 0.0 ],
                ]
            }}''')
    return "\n".join(out * 2)


class _Export:
    """A glTF the way Source2Viewer writes one: Source (x, y, z) as metres (y, z, x)."""

    def __init__(self, image_dir):
        self.dir = Path(image_dir)
        self.blob = bytearray()
        self.g = {"accessors": [], "bufferViews": [], "buffers": [{"byteLength": 0}], "nodes": [],
                  "meshes": [], "materials": [], "textures": [], "images": [],
                  "scenes": [{"nodes": []}], "scene": 0}

    def _accessor(self, array, component, kind):
        array = np.asarray(array)
        self.g["bufferViews"].append({"buffer": 0, "byteOffset": len(self.blob), "byteLength": array.nbytes})
        self.blob.extend(array.tobytes())
        self.g["accessors"].append({"bufferView": len(self.g["bufferViews"]) - 1, "componentType": component,
                                    "count": len(array), "type": kind})
        return len(self.g["accessors"]) - 1

    def image(self, name, colour):
        Image.new("RGB", (4, 4), colour).save(self.dir / name)
        self.g["images"].append({"uri": name})
        self.g["textures"].append({"source": len(self.g["images"]) - 1})
        return {"index": len(self.g["textures"]) - 1}

    def material(self, name, base=None, orm=None, normal=None, factor=None, blend=False):
        material = {"name": name, "pbrMetallicRoughness": {}}
        pbr = material["pbrMetallicRoughness"]
        if base is not None:
            pbr["baseColorTexture"] = self.image(f"{name}_base.png", base)
        if orm is not None:
            pbr["metallicRoughnessTexture"] = self.image(f"{name}_orm.png", orm)
        if normal is not None:
            material["normalTexture"] = self.image(f"{name}_normal.png", normal)
        if factor is not None:
            pbr["baseColorFactor"] = factor
        if blend:
            material["alphaMode"] = "BLEND"
        self.g["materials"].append(material)
        return len(self.g["materials"]) - 1

    def mesh(self, group, source_points, material, uv=None, source_normal=(0.0, 0.0, 1.0)):
        """A triangle fan over `source_points`, given in Source units."""
        p = np.asarray(source_points, np.float32)
        count = len(p)
        attrs = {
            "POSITION": self._accessor(np.stack([p[:, 1], p[:, 2], p[:, 0]], 1) * M, 5126, "VEC3"),
            "NORMAL": self._accessor(np.tile([source_normal[1], source_normal[2], source_normal[0]],
                                             (count, 1)).astype(np.float32), 5126, "VEC3"),
            "TANGENT": self._accessor(np.tile([0.0, 0.0, 1.0, -1.0], (count, 1)).astype(np.float32), 5126, "VEC4"),
            "TEXCOORD_0": self._accessor(np.asarray(uv if uv is not None else np.zeros((count, 2)), np.float32),
                                         5126, "VEC2"),
        }
        fan = [v for i in range(1, count - 1) for v in (0, i, i + 1)]
        indices = self._accessor(np.array(fan, np.uint16), 5123, "SCALAR")
        self.g["meshes"].append({"primitives": [{"attributes": attrs, "indices": indices, "material": material}]})
        self.g["nodes"].append({"name": f"weapons\\x.vmdl_c.{group}", "mesh": len(self.g["meshes"]) - 1})
        self.g["scenes"][0]["nodes"].append(len(self.g["nodes"]) - 1)

    def build(self, dump, **kw):
        return pw.build_weapon(self.g, bytes(self.blob), self.dir, dump, **kw)


def _attr(result, blob, name):
    acc = result["accessors"][result["meshes"][0]["primitives"][0]["attributes"][name]]
    view = result["bufferViews"][acc["bufferView"]]
    width = {"VEC2": 2, "VEC3": 3, "VEC4": 4}[acc["type"]]
    return np.frombuffer(bytes(blob), np.float32, acc["count"] * width, view["byteOffset"]).reshape(-1, width)


def _triangles(result):
    prim = result["meshes"][0]["primitives"][0]
    return result["accessors"][prim["indices"]]["count"] // 3


QUAD = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 1.0, 0.0), (0.0, 1.0, 0.0)]


class WeaponFrameTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ex = _Export(tmp.name)
        self.mat = self.ex.material("gun", base=(200, 100, 50))

    def test_positions_land_in_the_root_bones_frame_in_metres(self):
        self.ex.mesh("body_hd", [(2.0, 3.0, 4.0), (12.0, 3.0, 4.0), (12.0, 4.0, 4.0)], self.mat)
        result, blob, _ = self.ex.build(_dump([("weapon_offset", "", (-2.0, 1.0, -3.0))]))
        got = _attr(result, blob, "POSITION") / M
        np.testing.assert_allclose(got[0], [0.0, 4.0, 1.0], atol=1e-3)
        np.testing.assert_allclose(got[1], [10.0, 4.0, 1.0], atol=1e-3)

    def test_normals_turn_with_the_model(self):
        # An up-facing surface in Source stays up-facing: +Z is up in the output too.
        self.ex.mesh("body_hd", QUAD, self.mat, source_normal=(0.0, 0.0, 1.0))
        result, blob, _ = self.ex.build(_dump([("weapon_offset", "", (0.0, 0.0, 0.0))]))
        np.testing.assert_allclose(_attr(result, blob, "NORMAL")[0], [0.0, 0.0, 1.0], atol=1e-6)

    def test_tangents_turn_too_and_keep_their_handedness(self):
        self.ex.mesh("body_hd", QUAD, self.mat)
        result, blob, _ = self.ex.build(_dump([("weapon_offset", "", (0.0, 0.0, 0.0))]))
        # The export's +Z is Source +X, the barrel.
        np.testing.assert_allclose(_attr(result, blob, "TANGENT")[0], [1.0, 0.0, 0.0, -1.0], atol=1e-6)

    def test_triangle_winding_survives(self):
        # Viewed from above (+Z) the fan runs counter-clockwise, so its cross product points up.
        self.ex.mesh("body_hd", [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)], self.mat)
        result, blob, _ = self.ex.build(_dump([("weapon_offset", "", (0.0, 0.0, 0.0))]))
        a, b, c = _attr(result, blob, "POSITION")
        self.assertGreater(np.cross(b - a, c - a)[2], 0)

    def test_the_muzzle_is_in_the_same_frame_as_the_mesh(self):
        self.ex.mesh("body_hd", QUAD, self.mat)
        dump = _dump([("weapon_offset", "", (-3.0, 0.0, -3.0))],
                     [("muzzle_flash", "weapon_offset", (22.0, 0.5, -1.0))])
        result, _, stats = self.ex.build(dump)
        node = next(n for n in result["nodes"] if n["name"] == "muzzle_flash")
        np.testing.assert_allclose(np.array(node["translation"]) / M, [22.0, 0.5, -1.0], atol=1e-6)
        self.assertIn("muzzle_flash", stats["attachments"])

    def test_an_attachment_on_a_child_bone_goes_through_the_model_frame(self):
        self.ex.mesh("body_hd", QUAD, self.mat)
        # The slide sits 5 units further along the barrel than the root.
        dump = _dump([("weapon_offset", "", (0.0, 0.0, 0.0)), ("slide", "weapon_offset", (-5.0, 0.0, 0.0))],
                     [("shell_eject", "slide", (1.0, 0.0, 0.0))])
        result, _, _ = self.ex.build(dump)
        node = next(n for n in result["nodes"] if n["name"] == "shell_eject")
        np.testing.assert_allclose(np.array(node["translation"]) / M, [6.0, 0.0, 0.0], atol=1e-6)

    def test_grenades_have_no_muzzle_node(self):
        self.ex.mesh("body", QUAD, self.mat)
        result, _, _ = self.ex.build(_dump([("weapon_offset", "", (0.0, 0.0, 0.0))]))
        self.assertEqual([n["name"] for n in result["nodes"]], ["weapon"])


class MeshSelectionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ex = _Export(tmp.name)
        self.root = _dump([("weapon_offset", "", (0.0, 0.0, 0.0))])

    def test_the_default_mesh_group_wins_over_the_legacy_one(self):
        old = self.ex.material("old", base=(1, 1, 1))
        new = self.ex.material("new", base=(2, 2, 2))
        self.ex.mesh("body_legacy", QUAD + [(5.0, 5.0, 0.0)], old)
        self.ex.mesh("body_hd", QUAD, new)
        result, _, _ = self.ex.build(self.root)
        self.assertEqual(_triangles(result), 2)

    def test_a_knife_with_only_a_legacy_mesh_still_builds(self):
        self.ex.mesh("body_legacy", QUAD, self.ex.material("knife", base=(9, 9, 9)))
        result, _, _ = self.ex.build(self.root)
        self.assertEqual(_triangles(result), 2)

    def test_transparent_parts_are_dropped(self):
        self.ex.mesh("body_hd", QUAD, self.ex.material("gun", base=(1, 1, 1)))
        self.ex.mesh("body_hd", QUAD, self.ex.material("scope_lens", base=(1, 1, 1), blend=True))
        result, _, stats = self.ex.build(self.root)
        self.assertEqual((_triangles(result), stats["materials"]), (2, 1))

    def test_an_unknown_mesh_group_stops_the_build(self):
        self.ex.mesh("bodywork", QUAD, self.ex.material("gun", base=(1, 1, 1)))
        with self.assertRaises(SystemExit):
            self.ex.build(self.root)

    def test_a_single_material_may_tile_its_texture(self):
        uv = [(0.0, 0.0), (1.9, 0.0), (1.9, 1.0), (0.0, 1.0)]
        self.ex.mesh("body_hd", QUAD, self.ex.material("gun", base=(1, 1, 1)), uv=uv)
        result, blob, _ = self.ex.build(self.root)
        self.assertAlmostEqual(float(_attr(result, blob, "TEXCOORD_0")[:, 0].max()), 1.9, places=5)

    def test_two_materials_share_one_atlas_and_one_primitive(self):
        uv = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        self.ex.mesh("body_hd", QUAD, self.ex.material("gun", base=(1, 1, 1)), uv=uv)
        self.ex.mesh("body_hd", QUAD, self.ex.material("shell", base=(2, 2, 2)), uv=uv)
        result, blob, stats = self.ex.build(self.root)
        self.assertEqual(stats["materials"], 2)
        self.assertEqual(len(result["meshes"][0]["primitives"]), 1)
        self.assertLessEqual(float(_attr(result, blob, "TEXCOORD_0").max()), 1.0)
        self.assertAlmostEqual(float(_attr(result, blob, "TEXCOORD_0")[:, 0].max()), 0.5 + 0.5, places=5)

    def test_a_tiling_part_keeps_only_the_biggest_material(self):
        tiled = [(0.0, 0.0), (1.9, 0.0), (1.9, 1.0), (0.0, 1.0)]
        self.ex.mesh("body_hd", QUAD + [(0.0, 5.0, 0.0)], self.ex.material("gun", base=(1, 1, 1)), uv=tiled + [(0, 0)])
        self.ex.mesh("body_hd", QUAD, self.ex.material("small", base=(2, 2, 2)))
        result, _, stats = self.ex.build(self.root)
        self.assertEqual((stats["materials"], _triangles(result)), (1, 3))

    def test_the_material_name_is_not_one_the_viewer_hides(self):
        # scene.rs is_hidden_material despawns meshes by these substrings.
        source = (REPO / "rust/viewer/src/scene.rs").read_text(encoding="utf-8")
        hidden = re.findall(r'n\.contains\("(\w+)"\)', source)
        self.assertGreater(len(hidden), 3)
        for word in hidden:
            self.assertNotIn(word, pw._MATERIAL_NAME)
        self.assertFalse(pw._MATERIAL_NAME.startswith("tools"))


class DualPistolTest(unittest.TestCase):
    def test_only_the_right_pistol_stays_in_its_own_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = _Export(tmp)
            mat = ex.material("duals", base=(1, 1, 1))
            right = [(3.0, 0.0, 3.0), (10.0, 0.0, 3.0), (10.0, 1.0, 3.0)]
            left = [(3.0, 20.0, 3.0), (10.0, 20.0, 3.0), (10.0, 21.0, 3.0)]
            ex.mesh("body_hd", right + left, mat)
            # The fan from the first vertex would bridge the two guns, so index by hand.
            prim = ex.g["meshes"][0]["primitives"][0]
            prim["indices"] = ex._accessor(np.array([0, 1, 2, 3, 4, 5], np.uint16), 5123, "SCALAR")
            dump = _dump([("weapon", "", (-1.0, 0.0, 0.0)),
                          ("weapon_l", "weapon", (-3.0, -20.0, -3.0)),
                          ("weapon_r", "weapon", (-3.0, 0.0, -3.0))],
                         [("muzzle_flash", "weapon_r", (7.0, 0.0, 1.0))])
            result, blob, stats = ex.build(dump)
        self.assertEqual(stats["root"], "weapon_r")
        self.assertEqual(_triangles(result), 1)
        np.testing.assert_allclose(_attr(result, blob, "POSITION")[0] / M, [0.0, 0.0, 0.0], atol=1e-3)


class TextureTest(unittest.TestCase):
    def _pixel(self, result, blob, texture="baseColorTexture"):
        import io
        material = result["materials"][0]
        tex = (material["pbrMetallicRoughness"][texture] if texture == "baseColorTexture"
               else material[texture])
        view = result["bufferViews"][result["images"][result["textures"][tex["index"]]["source"]]["bufferView"]]
        data = bytes(blob[view["byteOffset"]:view["byteOffset"] + view["byteLength"]])
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))[2, 2].astype(int)

    def _build(self, **material):
        with tempfile.TemporaryDirectory() as tmp:
            ex = _Export(tmp)
            ex.mesh("body_hd", QUAD, ex.material("gun", **material))
            return ex.build(_dump([("weapon_offset", "", (0.0, 0.0, 0.0))]), base_size=8, normal_size=8,
                            jpeg_quality=100)

    def test_full_occlusion_darkens_to_the_floor_and_none_changes_nothing(self):
        result, blob, _ = self._build(base=(200, 200, 200), orm=(0, 128, 128))
        self.assertAlmostEqual(self._pixel(result, blob)[0], 200 * pw._AO_FLOOR, delta=3)
        result, blob, _ = self._build(base=(200, 200, 200), orm=(255, 128, 128))
        self.assertAlmostEqual(self._pixel(result, blob)[0], 200, delta=3)

    def test_the_base_colour_factor_applies(self):
        result, blob, _ = self._build(base=(200, 200, 200), factor=[0.5, 0.5, 0.5, 1.0])
        self.assertAlmostEqual(self._pixel(result, blob)[0], 100, delta=3)

    def test_a_missing_normal_map_reads_as_flat(self):
        result, blob, _ = self._build(base=(1, 1, 1))
        self.assertAlmostEqual(self._pixel(result, blob, "normalTexture")[2], 255, delta=3)


class GlbOutputTest(unittest.TestCase):
    def test_the_written_file_is_a_valid_glb(self):
        with tempfile.TemporaryDirectory() as tmp:
            ex = _Export(tmp)
            ex.mesh("body_hd", QUAD, ex.material("gun", base=(1, 1, 1)))
            result, blob, _ = ex.build(_dump([("weapon_offset", "", (0.0, 0.0, 0.0))]))
            result["buffers"][0]["byteLength"] = len(blob) + (-len(blob) % 4)
            out = Path(tmp) / "w.glb"
            pw.write_glb(out, result, blob)
            data = out.read_bytes()
        self.assertEqual(data[:4], b"glTF")
        self.assertEqual(struct.unpack("<I", data[8:12])[0], len(data))


if __name__ == "__main__":
    unittest.main()
