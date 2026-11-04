"""tools/precompress.py: what the site serves has to match its source."""
import gzip
import importlib.util
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("precompress", ROOT / "tools" / "precompress.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


def test_a_write_cut_short_leaves_nothing_that_looks_fresh():
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "world.c2map"
        src.write_bytes(b"map" * 1000)
        gz = src.with_name(src.name + ".gz")

        def restart(*_):
            raise OSError("the machine restarted")

        replace, pc.os.replace = pc.os.replace, restart
        try:
            try:
                pc.compress_one(src)
            except OSError:
                pass
            else:
                raise AssertionError("the write was meant to fail")
        finally:
            pc.os.replace = replace
        assert not pc.is_fresh(src, gz)
        assert not pc.is_fresh(src, src.with_name(src.name + ".br"))

        # the next run finishes the job over the leftover
        pc.compress_one(src)
        assert gzip.decompress(gz.read_bytes()) == src.read_bytes()
        assert not list(Path(d).glob("*.tmp"))


def test_the_site_serves_a_changed_file_not_its_old_copy():
    # a .js edited after the last build: its .br still holds the old code
    import os

    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.testclient import TestClient

    from web.app import CompressedStaticFiles

    with tempfile.TemporaryDirectory() as root:
        src = Path(root) / "settings.js"
        src.write_bytes(b"the old code")
        (Path(root) / "settings.js.br").write_bytes(b"small")
        client = TestClient(Starlette(routes=[Mount("/s", CompressedStaticFiles(directory=root))]))
        # HEAD, so the client doesn't try to unpack the fake brotli
        got = client.head("/s/settings.js", headers={"Accept-Encoding": "br"})
        assert got.headers["content-encoding"] == "br", "a fresh copy is served"

        src.write_bytes(b"the new code, longer")
        later = src.stat().st_mtime + 5
        os.utime(src, (later, later))
        got = client.head("/s/settings.js", headers={"Accept-Encoding": "br"})
        assert "content-encoding" not in got.headers
        assert got.headers["content-length"] == str(len(b"the new code, longer"))
