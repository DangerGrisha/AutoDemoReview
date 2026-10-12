"""The two games of the site and the switch between them.

CS2 lives in web/app.py (demos), World of Tanks Blitz in web/blitz.py
(replays, under /blitz). The top bar's switch goes through /game/{name}; the
session remembers the game the user was last in, so the landing page, the
Steam login and the brand link come back to it. Kept apart from app.py so it
imports nothing heavy (app.py pulls in the CS2 parser).
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

# Each game's home, and which pages belong to which game.
GAMES = {"cs2": "/profile", "blitz": "/blitz"}
_PAGES = (("/blitz", "blitz"), ("/profile", "cs2"), ("/stats", "cs2"),
          ("/upload", "cs2"), ("/connect", "cs2"), ("/matches", "cs2"))

router = APIRouter()


def game_of(path):
    """The game a page belongs to, None for shared pages (landing, login)."""
    for prefix, game in _PAGES:
        if path == prefix or path.startswith(prefix + "/"):
            return game
    return None


def home(session):
    """The home page of the game the user was last in (CS2 by default)."""
    return GAMES.get(session.get("game"), GAMES["cs2"])


class RememberGame:
    """Keep session["game"] on the game whose pages the user is on. Pure ASGI
    and registered BEFORE SessionMiddleware, which therefore wraps it: the
    session is loaded when this runs and saved after."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            game, session = game_of(scope.get("path", "")), scope.get("session")
            if game and session is not None and session.get("game") != game:
                session["game"] = game
        await self.app(scope, receive, send)


@router.get("/game/{name}")
def choose_game(request: Request, name: str):
    """The top bar's switch: remember the choice, go to that game's home."""
    if name not in GAMES:
        raise HTTPException(status_code=404)
    request.session["game"] = name
    signed_in = bool(request.session.get("steamid"))
    return RedirectResponse(GAMES[name] if signed_in else "/", status_code=302)
