"""Keeping a gzipped copy of each demo (web/ingest.py keep_demo).

What a user would notice: a match that can never gain a better replay because its
demo is gone, a disk error failing an upload that had already parsed fine, or a
half-written copy that later looks like a real demo.
"""

import gzip
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("SESSION_SECRET", "test-secret-for-the-keep-demo-tests-0123456789")

try:
    from web import config, ingest
except ImportError as exc:  # the analysis suite must still run without the web stack
    raise unittest.SkipTest(f"needs the web stack: {exc}")

DEMO = b"PBDEMS2\x00" + b"round data " * 5000


class KeepDemoTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.demos = self.root / "demos"
        self.demos.mkdir()
        self.dem = self.root / "upload.dem"
        self.dem.write_bytes(DEMO)
        for attr, value in (("DEMOS_DIR", self.demos), ("KEEP_DEMOS", True)):
            patcher = mock.patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_copy_unpacks_to_the_original_demo(self):
        path = ingest.keep_demo(self.dem, 27)
        self.assertEqual(path, self.demos / "27.dem.gz")
        self.assertEqual(gzip.decompress(path.read_bytes()), DEMO)
        self.assertLess(path.stat().st_size, len(DEMO))

    def test_nothing_is_kept_when_turned_off(self):
        with mock.patch.object(config, "KEEP_DEMOS", False):
            self.assertIsNone(ingest.keep_demo(self.dem, 27))
        self.assertEqual(list(self.demos.iterdir()), [])

    def test_an_existing_copy_is_not_rewritten(self):
        first = ingest.keep_demo(self.dem, 27)
        first.write_bytes(b"sentinel")
        self.assertEqual(ingest.keep_demo(self.dem, 27), first)
        self.assertEqual(first.read_bytes(), b"sentinel")

    def test_a_failure_returns_none_and_leaves_no_file_behind(self):
        # The source vanishes after the temp file was created: the realistic mid-copy
        # failure, and the case that must not leave a truncated "<id>.dem.gz".
        self.assertIsNone(ingest.keep_demo(self.root / "missing.dem", 27))
        self.assertEqual(list(self.demos.iterdir()), [])

    def test_an_unwritable_store_does_not_raise(self):
        with mock.patch.object(config, "DEMOS_DIR", self.root / "no" / "such" / "dir"):
            self.assertIsNone(ingest.keep_demo(self.dem, 27))


class IngestKeepsDemoTest(unittest.TestCase):
    """The two ways into ingest_demo that hold a demo must both keep it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.dem = self.root / "upload.dem"
        self.dem.write_bytes(DEMO)
        for attr, value in (("DEMOS_DIR", self.root), ("KEEP_DEMOS", True)):
            patcher = mock.patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _ingest_duplicate(self, can_access):
        with mock.patch.object(ingest.db, "find_match_by_hash", return_value=7), \
                mock.patch.object(ingest.db, "can_access", return_value=can_access), \
                mock.patch.object(ingest.replay3d, "has_replay", return_value=True):
            return ingest.ingest_demo(self.dem, owner_steamid="1", filename="x.dem",
                                      digest="abc")

    def test_reuploading_a_known_match_recovers_its_demo(self):
        res = self._ingest_duplicate(can_access=True)
        self.assertTrue(res.duplicate)
        self.assertEqual(gzip.decompress((self.root / "7.dem.gz").read_bytes()), DEMO)

    def test_a_stranger_reuploading_it_still_gets_no_link(self):
        res = self._ingest_duplicate(can_access=False)
        self.assertFalse(res.visible)
        self.assertTrue((self.root / "7.dem.gz").exists())

    def test_a_new_match_keeps_its_demo_even_when_the_3d_extract_fails(self):
        summary = {"my_final": 13, "opp_final": 9, "won_match": True, "n_rounds": 22,
                   "ref_kills": 1, "ref_deaths": 1}
        with mock.patch.object(ingest.db, "find_match_by_hash", return_value=None), \
                mock.patch.object(ingest.pipeline, "parse_demo_file",
                                  return_value=(summary, [], {})), \
                mock.patch.object(ingest.pipeline, "dumps_enriched", return_value="{}"), \
                mock.patch.object(ingest.participants, "extract_participants",
                                  return_value=[]), \
                mock.patch.object(ingest.db, "insert_match_with_participants",
                                  return_value=8), \
                mock.patch.object(ingest.replay3d, "extract", side_effect=RuntimeError):
            res = ingest.ingest_demo(self.dem, owner_steamid="1", filename="x.dem",
                                     digest="abc")
        self.assertEqual(res.match_id, 8)
        self.assertTrue((self.root / "8.dem.gz").exists())


if __name__ == "__main__":
    unittest.main()
