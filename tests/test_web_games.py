"""Tests for the game switch between CS2 and World of Tanks Blitz (web/games.py,
the top bar in web/templates/base.html).

What a user would notice: the switch leading somewhere else than the game
they picked, the site forgetting the game after a login or a sign-out, a
Blitz page showing the CS2 menu, or another game's page silently flipping the
remembered choice back. A minimal app (the switch, the Blitz router, the same
middleware order as web/app.py) runs on its own temporary database, so the
CS2 parser is never imported.
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("SESSION_SECRET", "test-secret-for-the-game-switch-tests-0123456789")

try:
    from fastapi import FastAPI, Request
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.testclient import TestClient
except ImportError:  # the analysis suite must still run without the web stack
    FastAPI = None

ALICE = "76561198000000001"


class _Site:
    def __enter__(self):
        if FastAPI is None:
            raise unittest.SkipTest("fastapi/starlette not installed")
        from web import blitz, config, db, games

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._old_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / "app.db"
        db.init_schema()
        db.upsert_user(ALICE, "Alice", None)

        app = FastAPI()
        app.add_middleware(games.RememberGame)     # same order as web/app.py
        app.add_middleware(SessionMiddleware, secret_key="test")
        app.include_router(games.router)
        app.include_router(blitz.router)

        @app.get("/_login/{sid}")
        def _login(request: Request, sid: str):
            request.session["steamid"] = sid
            return {"ok": True}

        @app.get("/_session")
        def _session(request: Request):
            return dict(request.session)

        @app.get("/profile")  # stands in for the CS2 page
        def _profile(request: Request):
            return {"game": request.session.get("game")}

        self.games = games
        self.client = TestClient(app)
        return self

    def __exit__(self, *exc):
        from web import config
        config.DB_PATH = self._old_db
        self._tmp.cleanup()

    def session(self):
        return self.client.get("/_session").json()


def test_pages_belong_to_their_game():
    if FastAPI is None:
        raise unittest.SkipTest("fastapi/starlette not installed")
    from web import games
    assert games.game_of("/blitz") == "blitz" and games.game_of("/blitz/career") == "blitz"
    assert games.game_of("/matches/12/3d") == "cs2" and games.game_of("/profile") == "cs2"
    assert games.game_of("/") is None and games.game_of("/login") is None
    assert games.game_of("/blitzkrieg") is None   # a prefix, not a page of Blitz
    assert games.home({}) == "/profile" and games.home({"game": "blitz"}) == "/blitz"


def test_the_switch_remembers_and_goes_home():
    with _Site() as site:
        # signed out: remembered, back to the landing page
        resp = site.client.get("/game/blitz", follow_redirects=False)
        assert resp.status_code == 302 and resp.headers["location"] == "/"
        assert site.session()["game"] == "blitz"
        site.client.get(f"/_login/{ALICE}")
        resp = site.client.get("/game/cs2", follow_redirects=False)
        assert resp.headers["location"] == "/profile" and site.session()["game"] == "cs2"
        assert site.client.get("/game/quake", follow_redirects=False).status_code == 404


def test_visiting_a_games_pages_switches_to_it():
    with _Site() as site:
        site.client.get(f"/_login/{ALICE}")
        site.client.get("/blitz")
        assert site.session()["game"] == "blitz"
        assert site.client.get("/profile").json() == {"game": "cs2"}  # set before the page runs
        site.client.get("/_session")
        assert site.session()["game"] == "cs2"                        # shared pages keep it


def test_blitz_pages_show_the_blitz_menu_and_the_switch():
    with _Site() as site:
        site.client.get(f"/_login/{ALICE}")
        page = site.client.get("/blitz").text
        assert 'href="/game/blitz" class="on"' in page and 'href="/game/cs2" class=""' in page
        assert '<a href="/blitz/career">Карьера</a>' in page and 'href="/upload"' not in page
        assert '<html lang="ru">' in page
