"""Replays that can be rebuilt and are sent compressed (web/replay3d.py, tools/rebuild_replays.py).

What a user would notice: a good replay lost to a failed rebuild, half a new replay mixed with
half an old one, an old replay that a re-upload does not improve, a rebuild from the wrong demo,
or a replay that downloads uncompressed or garbled.
"""

import gzip
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SESSION_SECRET", "test-secret-for-the-replay-data-tests-0123456789")

try:
    import brotli

    from web import config, ingest, replay3d
except ImportError as exc:  # the analysis suite must still run without the web stack
    raise unittest.SkipTest(f"needs the web stack: {exc}")

REPO = Path(__file__).resolve().parent.parent


def load_rebuild_tool():
    spec = importlib.util.spec_from_file_location("rebuild_replays", REPO / "tools" / "rebuild_replays.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.replays = self.root / "replay3d"
        self.replays.mkdir()
        self.bin = self.root / "extract3d.exe"
        self.bin.write_bytes(b"")
        for attr, value in (("REPLAY3D_DIR", self.replays), ("EXTRACT3D_BIN", self.bin)):
            patcher = mock.patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_replay(self, match_id, version=None, files=("r01.3dr", "r05.3dr")):
        out = self.replays / str(match_id)
        out.mkdir()
        manifest = {"map": "de_mirage", "rounds": []}
        if version is not None:
            manifest["dataVersion"] = version
        (out / "manifest.json").write_text(json.dumps(manifest), "utf-8")
        for name in files:
            (out / name).write_bytes(b"old " + name.encode())
        return out

    def built(self):
        """What is left around the final directories."""
        return [p.name for p in self.replays.iterdir() if p.name.startswith(".")]


def fake_extract3d(files=("r01.3dr",), returncode=0, manifest=True):
    """A stand-in for running the binary: writes into the --out-dir it is given."""
    def run(cmd, **_kwargs):
        out = Path(cmd[cmd.index("--out-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            (out / name).write_bytes(b"new " + name.encode())
        if manifest:
            (out / "manifest.json").write_text(
                json.dumps({"dataVersion": replay3d.DATA_VERSION}), "utf-8")
        return subprocess.CompletedProcess(cmd, returncode, "", "boom" if returncode else "")
    return run


class ExtractTest(Base):
    def test_a_first_replay_lands_whole(self):
        with mock.patch.object(replay3d.subprocess, "run", fake_extract3d()):
            self.assertTrue(replay3d.extract("x.dem", 7))
        self.assertTrue(replay3d.has_replay(7))
        self.assertEqual((self.replays / "7" / "r01.3dr").read_bytes(), b"new r01.3dr")
        self.assertEqual(self.built(), [])

    def test_a_rebuild_replaces_the_whole_replay(self):
        self.write_replay(7, files=("r01.3dr", "r30.3dr"))
        with mock.patch.object(replay3d.subprocess, "run", fake_extract3d()):
            self.assertTrue(replay3d.extract("x.dem", 7))
        self.assertEqual((self.replays / "7" / "r01.3dr").read_bytes(), b"new r01.3dr")
        self.assertFalse((self.replays / "7" / "r30.3dr").exists(), "no round of the old one stays")
        self.assertEqual(self.built(), [])

    def assert_old_replay_survives(self, run):
        self.write_replay(7)
        with mock.patch.object(replay3d.subprocess, "run", run):
            self.assertFalse(replay3d.extract("x.dem", 7))
        self.assertEqual((self.replays / "7" / "r05.3dr").read_bytes(), b"old r05.3dr")
        self.assertTrue(replay3d.has_replay(7))
        self.assertEqual(self.built(), [], "the failed build is cleaned up")

    def test_a_crash_keeps_the_old_replay(self):
        self.assert_old_replay_survives(fake_extract3d(returncode=3))

    def test_a_timeout_keeps_the_old_replay(self):
        self.assert_old_replay_survives(mock.Mock(side_effect=subprocess.TimeoutExpired("x", 1)))

    def test_no_manifest_keeps_the_old_replay(self):
        self.assert_old_replay_survives(fake_extract3d(manifest=False))

    def test_a_first_failure_leaves_nothing(self):
        with mock.patch.object(replay3d.subprocess, "run", fake_extract3d(returncode=3)):
            self.assertFalse(replay3d.extract("x.dem", 7))
        self.assertEqual(list(self.replays.iterdir()), [])
        self.assertFalse(replay3d.has_replay(7))

    def test_a_missing_binary_touches_nothing(self):
        self.write_replay(7)
        self.bin.unlink()
        self.assertFalse(replay3d.extract("x.dem", 7))
        self.assertEqual((self.replays / "7" / "r01.3dr").read_bytes(), b"old r01.3dr")

    def test_a_replay_in_use_stays_when_it_cannot_be_moved(self):
        # Windows refuses to rename a directory while a file in it is being sent.
        self.write_replay(7)
        real_replace = os.replace

        def replace(src, dst):
            if Path(src).name == "7":
                raise PermissionError("in use")
            return real_replace(src, dst)

        with mock.patch.object(replay3d.subprocess, "run", fake_extract3d()), \
                mock.patch.object(replay3d.os, "replace", replace):
            self.assertFalse(replay3d.extract("x.dem", 7))
        self.assertEqual((self.replays / "7" / "r01.3dr").read_bytes(), b"old r01.3dr")
        self.assertEqual(self.built(), [])

    def test_the_old_replay_comes_back_when_the_new_one_cannot_move_in(self):
        self.write_replay(7)
        real_replace = os.replace

        def replace(src, dst):
            if Path(dst).name == "7" and ".old" not in Path(src).name:
                raise PermissionError("disk trouble")
            return real_replace(src, dst)

        with mock.patch.object(replay3d.subprocess, "run", fake_extract3d()), \
                mock.patch.object(replay3d.os, "replace", replace):
            self.assertFalse(replay3d.extract("x.dem", 7))
        self.assertEqual((self.replays / "7" / "r01.3dr").read_bytes(), b"old r01.3dr")
        self.assertEqual(self.built(), [])

    def test_builds_left_by_a_killed_server_are_swept_but_not_a_running_one(self):
        old = self.replays / ".build-3-dead"
        running = self.replays / ".build-4-alive"
        old.mkdir()
        running.mkdir()
        long_ago = time.time() - 2 * config.EXTRACT3D_TIMEOUT_S - 60
        os.utime(old, (long_ago, long_ago))
        with mock.patch.object(replay3d.subprocess, "run", fake_extract3d()):
            replay3d.extract("x.dem", 7)
        self.assertFalse(old.exists())
        self.assertTrue(running.exists())


class StaleTest(Base):
    def test_the_extractor_and_the_site_agree_on_the_version(self):
        source = (REPO / "rust" / "extract" / "src" / "lib.rs").read_text("utf-8")
        version = re.search(r"pub const DATA_VERSION: i64 = (\d+);", source)
        self.assertEqual(int(version.group(1)), replay3d.DATA_VERSION)

    def test_no_replay_is_stale(self):
        self.assertIsNone(replay3d.data_version(7))
        self.assertTrue(replay3d.is_stale(7))

    def test_a_manifest_without_a_version_is_version_one(self):
        self.write_replay(7)
        self.assertEqual(replay3d.data_version(7), 1)
        self.assertTrue(replay3d.is_stale(7))

    def test_a_current_replay_is_not_stale(self):
        self.write_replay(7, version=replay3d.DATA_VERSION)
        self.assertFalse(replay3d.is_stale(7))

    def test_a_newer_replay_is_not_stale(self):
        self.write_replay(7, version=replay3d.DATA_VERSION + 1)
        self.assertFalse(replay3d.is_stale(7))

    def test_a_broken_manifest_is_stale(self):
        out = self.write_replay(7)
        for text in ("{not json", "[1, 2]"):
            (out / "manifest.json").write_text(text, "utf-8")
            self.assertTrue(replay3d.is_stale(7), text)


class ReuploadTest(Base):
    def setUp(self):
        super().setUp()
        self.dem = self.root / "upload.dem"
        self.dem.write_bytes(b"PBDEMS2\x00")
        for attr, value in (("DEMOS_DIR", self.root), ("KEEP_DEMOS", False)):
            patcher = mock.patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def reupload(self, can_access=True):
        with mock.patch.object(ingest.db, "find_match_by_hash", return_value=7), \
                mock.patch.object(ingest.db, "can_access", return_value=can_access), \
                mock.patch.object(ingest.replay3d, "extract") as extract:
            ingest.ingest_demo(self.dem, owner_steamid="1", filename="x.dem", digest="abc")
        return extract

    def test_an_old_replay_is_rebuilt(self):
        self.write_replay(7, version=1)
        self.reupload().assert_called_once_with(self.dem, 7)

    def test_a_missing_replay_is_built(self):
        self.reupload().assert_called_once_with(self.dem, 7)

    def test_a_current_replay_is_left_alone(self):
        self.write_replay(7, version=replay3d.DATA_VERSION)
        self.reupload().assert_not_called()

    def test_someone_else_cannot_trigger_a_rebuild(self):
        self.write_replay(7, version=1)
        self.reupload(can_access=False).assert_not_called()


class CompressionTest(Base):
    DATA = b"round data " * 5000

    def setUp(self):
        super().setUp()
        self.out = self.write_replay(7)
        self.path = self.out / "r01.3dr"
        self.path.write_bytes(self.DATA)

    def test_brotli_is_preferred_and_unpacks_to_the_file(self):
        copy, encoding = replay3d.compressed_artifact(self.path, {"gzip", "br"})
        self.assertEqual(encoding, "br")
        self.assertEqual(copy.name, "r01.3dr.br")
        self.assertEqual(brotli.decompress(copy.read_bytes()), self.DATA)
        self.assertLess(copy.stat().st_size, len(self.DATA))

    def test_gzip_for_a_client_without_brotli(self):
        copy, encoding = replay3d.compressed_artifact(self.path, {"gzip"})
        self.assertEqual(encoding, "gzip")
        self.assertEqual(gzip.decompress(copy.read_bytes()), self.DATA)

    def test_a_client_accepting_neither_gets_the_file(self):
        self.assertIsNone(replay3d.compressed_artifact(self.path, set()))
        self.assertIsNone(replay3d.compressed_artifact(self.path, {"identity"}))

    def test_a_copy_is_made_once(self):
        copy, _ = replay3d.compressed_artifact(self.path, {"br"})
        made = copy.stat().st_mtime_ns
        with mock.patch.object(replay3d, "COMPRESSIONS", {"br": (".br", self.fail)}):
            again, _ = replay3d.compressed_artifact(self.path, {"br"})
        self.assertEqual(again.stat().st_mtime_ns, made)

    def test_a_changed_file_gets_a_new_copy(self):
        copy, _ = replay3d.compressed_artifact(self.path, {"br"})
        past = time.time() - 100
        os.utime(copy, (past, past))
        self.path.write_bytes(b"changed")
        copy, _ = replay3d.compressed_artifact(self.path, {"br"})
        self.assertEqual(brotli.decompress(copy.read_bytes()), b"changed")

    def test_a_failure_falls_back_to_the_file_and_leaves_nothing(self):
        broken = {"br": (".br", mock.Mock(side_effect=ValueError("no")))}
        with mock.patch.object(replay3d, "COMPRESSIONS", broken):
            self.assertIsNone(replay3d.compressed_artifact(self.path, {"br"}))
        self.assertFalse(list(self.out.glob("*.tmp")))
        self.assertFalse((self.out / "r01.3dr.br").exists())

    def test_copies_are_not_artifacts(self):
        replay3d.compressed_artifact(self.path, {"br"})
        self.assertIsNone(replay3d.artifact_path(7, "r01.3dr.br"))

    def test_a_copy_is_not_taken_for_a_round(self):
        # tactics and the tests walk r*.3dr in the directory
        replay3d.compressed_artifact(self.path, {"br", "gzip"})
        names = sorted(p.name for p in self.out.glob("r*.3dr"))
        self.assertEqual(names, ["r01.3dr", "r05.3dr"])


class RouteTest(Base):
    def call(self, accept):
        try:
            from starlette.requests import Request

            from web import app as app_module
        except ImportError as exc:
            raise unittest.SkipTest(f"needs the web app: {exc}")
        out = self.write_replay(7)
        (out / "r01.3dr").write_bytes(b"round data " * 5000)
        scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
                 "headers": [(b"accept-encoding", accept)]}
        with mock.patch.object(app_module, "current_user", return_value={"steamid64": "1"}), \
                mock.patch.object(app_module.db, "get_match", return_value={"id": 7}):
            return app_module.match_replay3d_artifact(Request(scope), 7, "r01.3dr")

    def test_a_browser_gets_brotli(self):
        response = self.call(b"gzip, deflate, br")
        self.assertEqual(response.headers["content-encoding"], "br")
        self.assertEqual(response.headers["vary"], "Accept-Encoding")
        self.assertEqual(response.media_type, "application/octet-stream")
        self.assertEqual(brotli.decompress(Path(response.path).read_bytes()),
                         b"round data " * 5000)

    def test_a_client_refusing_compression_gets_the_file(self):
        response = self.call(b"br;q=0, gzip;q=0")
        self.assertNotIn("content-encoding", response.headers)
        self.assertTrue(response.path.endswith("r01.3dr"))


class RebuildToolTest(Base):
    def setUp(self):
        super().setUp()
        self.tool = load_rebuild_tool()
        self.demos = self.root / "demos"
        self.demos.mkdir()
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        for attr, value in (("DEMOS_DIR", self.demos), ("TMP_DIR", self.tmp), ("KEEP_DEMOS", True)):
            patcher = mock.patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def keep(self, match_id, data=b"demo bytes"):
        (self.demos / f"{match_id}.dem.gz").write_bytes(gzip.compress(data))

    def test_stale_replays_with_a_kept_demo_are_rebuilt(self):
        self.keep(1)
        self.keep(2)
        self.write_replay(1, version=1)
        self.write_replay(2, version=replay3d.DATA_VERSION)
        seen = []

        def extract(path, match_id):
            seen.append((match_id, Path(path).read_bytes()))
            return True

        with mock.patch.object(replay3d, "extract", extract):
            self.assertEqual(self.tool.main([]), 0)
        self.assertEqual(seen, [(1, b"demo bytes")], "unpacked for the run")
        self.assertEqual(list(self.tmp.iterdir()), [], "and the unpacked demo is gone")

    def test_force_rebuilds_current_ones_too(self):
        self.keep(2)
        self.write_replay(2, version=replay3d.DATA_VERSION)
        with mock.patch.object(replay3d, "extract", return_value=True) as extract:
            self.assertEqual(self.tool.main(["--force"]), 0)
        extract.assert_called_once()

    def test_a_failure_is_reported_and_the_rest_still_run(self):
        self.keep(1)
        self.keep(2)
        with mock.patch.object(replay3d, "extract", side_effect=[False, True]) as extract:
            self.assertEqual(self.tool.main([]), 1)
        self.assertEqual(extract.call_count, 2)

    def test_a_broken_copy_is_reported_and_the_rest_still_run(self):
        (self.demos / "1.dem.gz").write_bytes(b"not gzip")
        self.keep(2)
        with mock.patch.object(replay3d, "extract", return_value=True) as extract:
            self.assertEqual(self.tool.main([]), 1)
        self.assertEqual(extract.call_args[0][1], 2)

    def test_a_demo_goes_to_the_match_it_came_from(self):
        demo = self.root / "a.dem"
        demo.write_bytes(b"demo bytes")
        with mock.patch.object(self.tool.db, "find_match_by_hash", return_value=27) as find, \
                mock.patch.object(replay3d, "extract", return_value=True) as extract:
            self.assertEqual(self.tool.main([str(demo)]), 0)
        find.assert_called_once_with(ingest.sha256_file(demo))
        extract.assert_called_once_with(demo, 27)
        self.assertTrue((self.demos / "27.dem.gz").exists(), "and it is kept from now on")

    def test_a_demo_of_no_stored_match_changes_nothing(self):
        demo = self.root / "a.dem"
        demo.write_bytes(b"demo bytes")
        with mock.patch.object(self.tool.db, "find_match_by_hash", return_value=None), \
                mock.patch.object(replay3d, "extract") as extract:
            self.assertEqual(self.tool.main([str(demo)]), 1)
        extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
