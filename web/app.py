"""FastAPI application: Steam login, demo upload+parse, per-user match viz.

All route handlers are sync `def` so Starlette runs them in its threadpool --
the ~1-minute demo parse then never blocks the event loop. A match is visible to
any user who participated in it (their SteamID is a parsed player) or uploaded it.
"""

import hashlib
import html
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import career, config, db, participants, pipeline, steam_openid

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


@asynccontextmanager
async def lifespan(app):
    db.init_schema()
    yield


app = FastAPI(title="demoReview", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    same_site="lax",
    https_only=False,          # LAN / http
    max_age=30 * 24 * 3600,
)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


def current_user(request):
    """The logged-in user row, or None."""
    sid = request.session.get("steamid")
    return db.get_user(sid) if sid else None


def _page(request, name, ctx, status_code=200):
    # Starlette's current API: TemplateResponse(request, name, context, ...);
    # `request` is injected into the template context automatically.
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


# ---- public ----

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return _page(request, "landing.html", {"user": current_user(request)})


@app.get("/login")
def login(request: Request):
    if request.session.get("steamid"):
        return RedirectResponse("/profile", status_code=302)
    return RedirectResponse(steam_openid.build_login_url(config.BASE_URL), status_code=302)


@app.get("/auth/steam/callback")
def steam_callback(request: Request):
    params = dict(request.query_params)
    expected = f"{config.BASE_URL}/auth/steam/callback"
    steamid = steam_openid.verify_callback(params, expected_return_to=expected)
    if not steamid:
        return RedirectResponse("/?error=login_failed", status_code=302)
    persona, avatar = steam_openid.fetch_persona(steamid, config.STEAM_API_KEY)
    db.upsert_user(steamid, persona, avatar)
    request.session["steamid"] = steamid
    return RedirectResponse("/profile", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


# ---- logged-in ----

@app.get("/profile", response_class=HTMLResponse)
def profile(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    matches = db.list_matches(user["steamid64"])
    return _page(request, "profile.html", {"user": user, "matches": matches})


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    data = career.build_career(
        db.career_totals(user["steamid64"]), db.career_blobs(user["steamid64"]))
    return _page(request, "stats.html", {"user": user, "career": data})


@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _page(request, "upload.html", {"user": user, "error": None})


@app.post("/upload")
def upload_demo(request: Request, demo: UploadFile = File(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    def err(message, code=400):
        return _page(request, "upload.html", {"user": user, "error": message}, code)

    filename = (demo.filename or "upload.dem").strip() or "upload.dem"
    if not filename.lower().endswith(".dem"):
        return err("Please choose a .dem file.")

    clen = int(request.headers.get("content-length") or 0)
    if clen and clen > config.MAX_UPLOAD_BYTES:
        return err(f"File too large (limit {config.MAX_UPLOAD_MB} MB).", 413)

    tmp_path = None
    try:
        # Stream the upload to a temp .dem, hashing + size-guarding as we go.
        fd, tmp_name = tempfile.mkstemp(suffix=".dem", dir=str(config.TMP_DIR))
        tmp_path = Path(tmp_name)
        sha = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = demo.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    return err(f"File too large (limit {config.MAX_UPLOAD_MB} MB).", 413)
                sha.update(chunk)
                out.write(chunk)
        digest = sha.hexdigest()

        # Already analyzed (global dedup)? Skip the ~1-min parse. Redirect only if
        # the re-uploader may actually see it (a participant is already linked);
        # never leak a match uploaded by another group on a hash collision.
        existing = db.find_match_by_hash(digest)
        if existing is not None:
            if db.can_access(existing, user["steamid64"]):
                return RedirectResponse(f"/matches/{existing}?dup=1", status_code=302)
            return err("This demo has already been analyzed (uploaded by someone "
                       "else) and you don't appear in it.", 409)

        # Cheap sanity check before the expensive parse.
        with open(tmp_path, "rb") as f:
            if f.read(8)[:7] != b"PBDEMS2":
                return err("That doesn't look like a CS2 demo (bad file header).")

        summary, rounds, meta = pipeline.parse_demo_file(tmp_path, user["steamid64"])
        if not summary:
            return err("No scored rounds found in that demo (warm-up only or unsupported).")

        match = {
            "uploader_steamid": user["steamid64"],
            "original_filename": filename,
            "map_name": meta.get("map_name"),
            "my_score": summary["my_final"],
            "opp_score": summary["opp_final"],
            "won": 1 if summary["won_match"] else 0,
            "n_rounds": summary["n_rounds"],
            "ref_name": meta.get("ref_name"),
            "ref_sid": meta.get("ref_sid"),
            "ref_kills": summary["ref_kills"],
            "ref_deaths": summary["ref_deaths"],
            "found": 1 if meta.get("found") else 0,
            "dem_sha256": digest,
            "data_json": pipeline.dumps_enriched(summary, rounds, meta),
        }
        try:
            match_id = db.insert_match_with_participants(
                match=match, participants=participants.extract_participants(summary))
        except sqlite3.IntegrityError:
            # Lost a check-then-insert race with a concurrent identical upload (the
            # global UNIQUE(dem_sha256) index fired). Treat it as the dup it is.
            existing = db.find_match_by_hash(digest)
            if existing is not None and db.can_access(existing, user["steamid64"]):
                return RedirectResponse(f"/matches/{existing}?dup=1", status_code=302)
            raise
        return RedirectResponse(f"/matches/{match_id}", status_code=302)
    except Exception as exc:  # noqa: BLE001 -- surface any parse failure cleanly
        return err(f"Couldn't parse this file -- is it a valid CS2 .dem? ({type(exc).__name__})")
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()          # never keep the raw .dem
            except OSError:
                pass


@app.get("/matches/{match_id}", response_class=HTMLResponse)
def match_view(request: Request, match_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    row = db.get_match(match_id, user["steamid64"])       # access-scoped: participant or uploader
    if row is None:
        raise HTTPException(status_code=404)               # not a participant, not the uploader
    summary, rounds, meta = pipeline.loads_enriched(row["data_json"])
    avatar = (f'<img src="{html.escape(user["avatar_url"])}" alt="">'
              if user["avatar_url"] else "")
    # The viz is baked from the uploader's perspective; note it when someone else uploaded.
    note = ""
    if row["uploader_steamid"] != user["steamid64"]:
        up = db.get_user(row["uploader_steamid"])
        up_name = html.escape(up["persona_name"]) if up else "another player"
        note = f'<span class="sn-note">shown from {up_name}’s perspective</span>'
    site_nav = (
        '<div class="site-nav">'
        '<a href="/profile">&#8592; My matches</a>'
        '<span class="sn-brand">demoReview</span>'
        f'{note}'
        f'<span class="sn-user">{avatar}{html.escape(user["persona_name"])}</span>'
        '</div>'
    )
    page = pipeline.render_match_html(
        summary, rounds, row["original_filename"] or "match.dem", meta, site_nav=site_nav)
    return HTMLResponse(page)


# ---- errors ----

@app.exception_handler(StarletteHTTPException)
def on_http_exception(request: Request, exc: StarletteHTTPException):
    code = exc.status_code
    message = {
        404: "Not found -- this match doesn't exist, or it isn't yours.",
        403: "You don't have access to that.",
    }.get(code, exc.detail or "Something went wrong.")
    try:
        user = current_user(request)
    except Exception:  # noqa: BLE001
        user = None
    return _page(request, "error.html", {"user": user, "code": code, "message": message}, code)
