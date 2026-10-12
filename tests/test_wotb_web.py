"""Tests for the Blitz web pages (web/blitz.py + the wotb_battles table).

What can go wrong for a user: someone else's battle shows up in my list, the
same replay dropped twice becomes two battles, a later copy of a replay that
finally HAS the battle results is thrown away as a duplicate, a battle stored
by an older parser can never get the new report (the 2D replay) because its
file is "already uploaded", one bad file in a batch sinks the good ones, or
the schema migration breaks an existing database. Each test runs against its own temporary SQLite file and a minimal
app (just the Blitz router + sessions), so the CS2 pipeline is never imported.
"""

import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "test-secret-for-the-blitz-web-tests-0123456789")

try:
    from fastapi import FastAPI, Request
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.testclient import TestClient
except ImportError:  # the analysis suite must still run without the web stack
    FastAPI = None

FIXTURES = Path(__file__).resolve().parent / "data" / "wotb"
CANYON = FIXTURES / "v11_20_canyon_results.wotbreplay"
ALICE, BOB = "76561198000000001", "76561198000000002"


class _Site:
    """A fresh database + client; use as a context manager."""

    def __enter__(self):
        if FastAPI is None:
            raise unittest.SkipTest("fastapi/starlette not installed")
        from web import blitz, config, db

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._old_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "app.db"
        db.init_schema()
        db.upsert_user(ALICE, "Alice", None)
        db.upsert_user(BOB, "Bob", None)

        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test")
        app.include_router(blitz.router)

        @app.get("/_login/{sid}")
        def _login(request: Request, sid: str):
            request.session["steamid"] = sid
            return {"ok": True}

        self.db = db
        self.client = TestClient(app)
        return self

    def __exit__(self, *exc):
        from web import config
        config.DB_PATH = self._old_db
        self._tmp.cleanup()

    def login(self, sid):
        self.client.get(f"/_login/{sid}")

    def upload(self, *files, json_api=True):
        payload = [("replays", (name, data, "application/octet-stream")) for name, data in files]
        headers = {"Accept": "application/json"} if json_api else {}
        return self.client.post("/blitz/upload", files=payload, headers=headers,
                                follow_redirects=False)


def _without_results(path):
    """The same replay as a copy taken before the battle results were written."""
    out = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename != "battle_results.dat":
                dst.writestr(info.filename, src.read(info))
    return out.getvalue()


def test_blitz_pages_need_a_login():
    with _Site() as site:
        assert site.client.get("/blitz", follow_redirects=False).headers["location"] == "/login"
        assert site.upload(("a.wotbreplay", b"x")).status_code == 401


def test_upload_then_list_then_report():
    with _Site() as site:
        site.login(ALICE)
        res = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        assert res["ok"] and res["status"] == "new", res
        listing = site.client.get("/blitz").text
        assert "Каньон" in listing and f"/blitz/battles/{res['id']}" in listing
        report = site.client.get(f"/blitz/battles/{res['id']}").text
        assert "Kossmooss" in report and "Объект 704" in report and "Поражение" in report
        assert "Мои бои Blitz" in report  # the site nav made it in


def test_dropping_the_same_file_twice_keeps_one_battle():
    with _Site() as site:
        site.login(ALICE)
        first = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        again = site.upload(("renamed.wotbreplay", CANYON.read_bytes())).json()["results"][0]
        assert again["status"] == "duplicate" and again["id"] == first["id"]
        assert len(site.db.list_wotb_battles(ALICE)) == 1


def test_a_later_copy_with_results_upgrades_the_battle_in_place():
    with _Site() as site:
        site.login(ALICE)
        early = site.upload((CANYON.name, _without_results(CANYON))).json()["results"][0]
        assert early["status"] == "new"
        row = site.db.get_wotb_battle(early["id"], ALICE)
        assert row["has_results"] == 0 and row["outcome"] == "unknown"

        full = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        assert full["status"] == "updated" and full["id"] == early["id"]
        row = site.db.get_wotb_battle(early["id"], ALICE)
        assert row["has_results"] == 1 and row["outcome"] == "loss" and row["damage"] == 1399

        # ...but never the other way round: a thinner copy does not downgrade.
        late = site.upload(("copy.wotbreplay", _without_results(CANYON))).json()["results"][0]
        assert late["status"] == "duplicate"
        assert site.db.get_wotb_battle(early["id"], ALICE)["has_results"] == 1


def _store_as_format_1(site, battle_id):
    """Rewrite a stored battle the way the parser before the 2D replay left it."""
    conn = site.db.connect()
    try:
        data = json.loads(conn.execute("SELECT data_json FROM wotb_battles WHERE id = ?",
                                       (battle_id,)).fetchone()["data_json"])
        data["format"] = 1
        data.pop("replay", None)
        conn.execute("UPDATE wotb_battles SET data_json = ? WHERE id = ?",
                     (json.dumps(data), battle_id))
        conn.commit()
    finally:
        conn.close()


def test_a_battle_from_an_older_parser_is_reparsed_when_uploaded_again():
    with _Site() as site:
        site.login(ALICE)
        first = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        _store_as_format_1(site, first["id"])
        assert "загрузите реплей заново" in site.client.get(f"/blitz/battles/{first['id']}").text

        again = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        assert again["status"] == "updated" and again["id"] == first["id"], again
        stored = json.loads(site.db.get_wotb_battle(first["id"], ALICE)["data_json"])
        assert stored["format"] >= 2 and stored["replay"]["tracks"]
        assert 'id="replay-data"' in site.client.get(f"/blitz/battles/{first['id']}").text
        # current now: the same file is a plain duplicate again
        third = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        assert third["status"] == "duplicate"
        assert len(site.db.list_wotb_battles(ALICE)) == 1


def test_an_outdated_battle_never_trades_its_results_for_tracks():
    with _Site() as site:
        site.login(ALICE)
        first = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        _store_as_format_1(site, first["id"])
        thin = site.upload(("copy.wotbreplay", _without_results(CANYON))).json()["results"][0]
        assert thin["status"] == "duplicate", thin
        row = site.db.get_wotb_battle(first["id"], ALICE)
        assert row["has_results"] == 1 and row["damage"] == 1399


def test_other_users_cannot_see_a_battle():
    with _Site() as site:
        site.login(ALICE)
        res = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        site.login(BOB)
        assert site.client.get(f"/blitz/battles/{res['id']}").status_code == 404
        assert "Каньон" not in site.client.get("/blitz").text
        # Bob may still upload the same replay for himself.
        mine = site.upload((CANYON.name, CANYON.read_bytes())).json()["results"][0]
        assert mine["status"] == "new" and mine["id"] != res["id"]


def test_bad_files_fail_alone_in_a_batch():
    with _Site() as site:
        site.login(ALICE)
        results = site.upload(("notes.txt", b"hello"), ("fake.wotbreplay", b"PK\x03\x04junk"),
                              (CANYON.name, CANYON.read_bytes())).json()["results"]
        assert [r["ok"] for r in results] == [False, False, True], results
        assert "wotbreplay" in results[0]["error"] and "Blitz" in results[1]["error"]
        assert len(site.db.list_wotb_battles(ALICE)) == 1


def test_the_no_js_form_gets_a_rendered_page():
    with _Site() as site:
        site.login(ALICE)
        resp = site.upload((CANYON.name, CANYON.read_bytes()), json_api=False)
        assert resp.status_code == 200 and "новый бой" in resp.text


def test_migrating_a_v3_database_adds_the_table_and_keeps_matches():
    if FastAPI is None:
        raise unittest.SkipTest("web stack not installed")
    from web import db, migrations

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = sqlite3.connect(str(Path(tmp) / "old.db"))
        conn.row_factory = sqlite3.Row
        old_schema = db.SCHEMA.replace(db.WOTB_BATTLES_DDL, "")
        for stmt in old_schema.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        conn.execute("INSERT INTO users (steamid64, persona_name) VALUES ('1', 'x')")
        conn.execute("INSERT INTO matches (uploader_steamid, my_score, opp_score, won, n_rounds,"
                     " ref_kills, ref_deaths, found, data_json) VALUES ('1',13,7,1,20,20,10,1,'{}')")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()

        migrations.run(conn, db.SCHEMA)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == migrations.CURRENT_VERSION == 4
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "wotb_battles" in tables
        assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1
        conn.close()


RIFT = FIXTURES / "v11_20_rift_base_capture.wotbreplay"
HIMMELSDORF = FIXTURES / "v11_7_himmelsdorf_results.wotbreplay"  # recorded by another player


def test_the_career_keeps_each_recorders_battles_apart():
    with _Site() as site:
        site.login(ALICE)
        assert "Пока ни одного боя" in site.client.get("/blitz/career").text
        ids = [site.upload((p.name, p.read_bytes())).json()["results"][0]["id"]
               for p in (CANYON, RIFT, HIMMELSDORF)]
        assert "/blitz/career" in site.client.get("/blitz").text

        page = site.client.get("/blitz/career").text  # the account with the most battles
        assert "Карьера Blitz &mdash; _P1ratiC_" in page
        assert "Объект 704" in page and "ИСУ-130" in page and "T77" not in page
        assert f"/blitz/battles/{ids[0]}" in page and f"/blitz/battles/{ids[2]}" not in page
        assert "Погибли в отрыве от команды" in page  # canyon's coach, in the recent list
        assert "_oleg12054_" in page                  # ...and the other recorder to pick

        other = site.db.get_wotb_battle(ids[2], ALICE)["recorder_account"]
        page = site.client.get(f"/blitz/career?account={other}").text
        assert "Карьера Blitz &mdash; _oleg12054_" in page and "T77" in page
        assert "Объект 704" not in page

        site.login(BOB)  # nobody else's battles
        assert "Пока ни одного боя" in site.client.get("/blitz/career").text
