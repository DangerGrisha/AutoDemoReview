"""FastAPI application: Steam login, demo upload+parse, per-user match viz.

All route handlers are sync `def` so Starlette runs them in its threadpool --
the ~1-minute demo parse then never blocks the event loop. A match is visible to
any user who participated in it (their SteamID is a parsed player) or uploaded it.
"""

import hashlib
import html
import mimetypes
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import (autoimport, blitz, career, coaching, config, db, ingest, pipeline,
               replay3d, steam_openid)

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Windows' registry-backed mimetypes DB often lacks (or mislabels) these. The
# .wasm one is load-bearing: WebAssembly.instantiateStreaming refuses anything
# but application/wasm. Registering here fixes FileResponse's guess_type for
# compressed AND uncompressed serving alike.
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("model/gltf-binary", ".glb")
mimetypes.add_type("text/javascript", ".js")


class CompressedStaticFiles(StaticFiles):
    """StaticFiles + precompressed siblings + long-lived caching for replay3d.

    tools/precompress.py writes `<file>.br` / `<file>.gz` next to the big 3D
    replay payloads (wasm / glb / js). When the request's Accept-Encoding
    allows it and the sibling exists, the sibling is served instead, with:

      * Content-Encoding: br|gzip
      * the ORIGINAL Content-Type (application/wasm keeps
        WebAssembly.instantiateStreaming on the fast path)
      * Vary: Accept-Encoding

    Content-Length / ETag / Last-Modified / 304 revalidation all come from
    default StaticFiles behavior applied to the sibling file, so they are
    consistent per-variant. Everything under /static/replay3d/ additionally
    gets a week of Cache-Control (the page warm-fetches the map .glb so
    Bevy's own fetch of the same URL becomes a deterministic disk-cache hit).
    """

    COMPRESSIBLE_EXTS = {".wasm", ".glb", ".js", ".3dr"}
    MEDIA_TYPES = {
        ".wasm": "application/wasm",
        ".glb": "model/gltf-binary",
        ".js": "text/javascript",
        ".3dr": "application/octet-stream",
    }

    @staticmethod
    def _accepted_encodings(scope):
        """Lower-cased encoding tokens from Accept-Encoding, minus q=0 ones."""
        raw = b",".join(v for k, v in scope.get("headers", []) if k == b"accept-encoding")
        accepted = set()
        for part in raw.decode("latin-1").split(","):
            token, _, params = part.partition(";")
            token = token.strip().lower()
            if not token:
                continue
            q = params.strip().lower()
            if q.startswith("q="):
                try:
                    if float(q[2:].split(";")[0]) == 0.0:
                        continue
                except ValueError:
                    pass
            accepted.add(token)
        return accepted

    async def get_response(self, path, scope):
        ext = os.path.splitext(path)[1].lower()
        response = None
        if ext in self.COMPRESSIBLE_EXTS and scope["method"] in ("GET", "HEAD"):
            accepted = self._accepted_encodings(scope)
            for encoding, suffix in (("br", ".br"), ("gzip", ".gz")):
                if encoding not in accepted:
                    continue
                if self.lookup_path(path + suffix)[1] is None:   # no sibling on disk
                    continue
                response = await super().get_response(path + suffix, scope)
                if response.status_code == 200:
                    response.headers["content-encoding"] = encoding
                    response.headers["content-type"] = self.MEDIA_TYPES[ext]
                break
        if response is None:
            response = await super().get_response(path, scope)
        if ext in self.COMPRESSIBLE_EXTS:
            # Both variants of the URL exist -> caches must key on the header.
            response.headers["vary"] = "Accept-Encoding"
        if path.replace("\\", "/").startswith("replay3d/"):
            response.headers["cache-control"] = "public, max-age=604800"
        return response


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
app.mount("/static", CompressedStaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.include_router(blitz.router)   # World of Tanks Blitz: /blitz/...


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
    # dicts (not sqlite3.Row) so the 3D flags can ride along to the template
    matches = [dict(m) for m in db.list_matches(user["steamid64"])]
    for m in matches:
        m["has3d"] = replay3d.has_replay(m["id"])
        m["map3d"] = replay3d.map_asset_rel(m["map_name"]) is not None
    return _page(request, "profile.html", {"user": user, "matches": matches})


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    data = career.build_career(
        db.career_totals(user["steamid64"]), db.career_blobs(user["steamid64"]))
    return _page(request, "stats.html", {"user": user, "career": data})


@app.get("/connect", response_class=HTMLResponse)
def connect_form(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _page(request, "connect.html",
                 {"user": user, "st": autoimport.status(user["steamid64"]),
                  "error": None, "saved": request.query_params.get("saved") == "1"})


@app.post("/connect", response_class=HTMLResponse)
def connect_save(request: Request,
                 auth_code: str = Form(""),
                 share_code: str = Form(""),
                 chat_optin: str = Form("")):
    """Store an auto-import subscription after validating the codes against Steam."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    try:
        autoimport.connect(user["steamid64"], auth_code, share_code,
                           chat_optin=bool(chat_optin))
    except autoimport.SetupError as exc:
        st = autoimport.status(user["steamid64"])
        # Keep what they typed (minus the secret) so the form isn't a retype.
        st["form_sharecode"] = (share_code or "").strip()
        return _page(request, "connect.html",
                     {"user": user, "st": st, "error": str(exc), "saved": False}, 400)
    return RedirectResponse("/connect?saved=1", status_code=302)


@app.post("/connect/off")
def connect_off(request: Request):
    """Stop auto-import and forget the stored auth code."""
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    db.delete_steam_link(user["steamid64"])
    return RedirectResponse("/connect", status_code=302)


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

        # Dedup, parse, insert and 3D extract all live in web/ingest.py -- the Steam
        # bot's auto-download worker goes through the very same function.
        try:
            res = ingest.ingest_demo(tmp_path, owner_steamid=user["steamid64"],
                                     filename=filename, digest=digest)
        except ingest.IngestError as exc:
            return err(str(exc))
        if not res.visible:
            # Someone else's match on a hash hit: never link a stranger to it.
            return err("This demo has already been analyzed (uploaded by someone "
                       "else) and you don't appear in it.", 409)
        suffix = "?dup=1" if res.duplicate else ""
        return RedirectResponse(f"/matches/{res.match_id}{suffix}", status_code=302)
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
    # A pill, not a plain link -- the 3D replay is a headline feature and was
    # getting lost among the nav's text links. The title flags the fallback when
    # this map has no converted geometry yet (the viewer then shows the radar).
    if replay3d.has_replay(match_id):
        has_map = replay3d.map_asset_rel(row["map_name"]) is not None
        tip = ("Watch this round in 3D on the real map geometry" if has_map else
               "Watch this round in 3D -- this map isn't converted yet, "
               "so the viewer falls back to the flat radar")
        replay3d_link = (f'<a class="sn-3d" href="/matches/{match_id}/3d" '
                         f'title="{html.escape(tip)}">&#9654; Watch in 3D</a>')
    else:
        replay3d_link = ""
    site_nav = (
        '<div class="site-nav">'
        '<a href="/profile">&#8592; My matches</a>'
        f'{replay3d_link}'
        '<span class="sn-brand">demoReview</span>'
        f'{note}'
        f'<span class="sn-user">{avatar}{html.escape(user["persona_name"])}</span>'
        '</div>'
    )
    # AI coaching: cached per (match, viewer) -- the report is about whoever is
    # looking at it, not about the uploader. `error` comes back as a query param
    # from a failed generate so the message lands on the card, not a blank page.
    coaching_md = coaching.markdown(match_id, user["steamid64"])
    error = request.query_params.get("coach_error")
    label = "Regenerate" if coaching_md else "Generate report"
    coaching_action = (
        (f'<span class="coach-err">{html.escape(error)}</span>' if error else "")
        + f'<form method="post" action="/matches/{match_id}/coach" '
          'class="coach-form" onsubmit="this.querySelector(\'button\').disabled=true;'
          'this.querySelector(\'button\').textContent=\'Analysing\\u2026\';">'
          f'<button class="coach-btn coach-gen" type="submit">{label}</button>'
          '</form>'
    )
    page = pipeline.render_match_html(
        summary, rounds, row["original_filename"] or "match.dem", meta,
        site_nav=site_nav, coaching_md=coaching_md, coaching_action=coaching_action)
    return HTMLResponse(page)


@app.post("/matches/{match_id}/coach")
def match_coach(request: Request, match_id: int):
    """Generate (or refresh) this viewer's coaching report, then show the match.

    Synchronous on purpose: the handler runs in Starlette's threadpool and the
    call takes under a minute, so the user gets the finished report on the
    redirect instead of a job to poll.
    """
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    row = db.get_match(match_id, user["steamid64"])       # access-scoped, like match_view
    if row is None:
        raise HTTPException(status_code=404)
    try:
        coaching.generate(match_id, user["steamid64"], row["data_json"])
        target = f"/matches/{match_id}"
    except coaching.CoachUnavailable as exc:
        target = f"/matches/{match_id}?coach_error={quote(str(exc)[:200])}"
    return RedirectResponse(target, status_code=303)


# ---- 3D replay ----

@app.get("/matches/{match_id}/3d", response_class=HTMLResponse)
def match_replay3d(request: Request, match_id: int):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    row = db.get_match(match_id, user["steamid64"])       # access-scoped, like match_view
    if row is None:
        raise HTTPException(status_code=404)
    map_asset = replay3d.map_asset_rel(row["map_name"])
    # UNCOMPRESSED byte sizes for the loading progress bars: with
    # Content-Encoding active, Content-Length is the compressed size while the
    # page's stream reader counts decompressed bytes -- so the page needs the
    # real denominators from us. 0 = unknown (bars fall back to indeterminate).
    def _size(p):
        try:
            return p.stat().st_size
        except OSError:
            return 0
    wasm_size = _size(WEB_DIR / "static" / "replay3d" / "pkg" / "viewer_bg.wasm")
    map_size = _size(WEB_DIR / "static" / "replay3d" / "assets" / Path(map_asset)) if map_asset else 0
    return _page(request, "replay3d.html", {
        "user": user,
        "match_id": match_id,
        "map_name": row["map_name"] or "",
        "demo_name": row["original_filename"] or "match.dem",
        "has_replay": replay3d.has_replay(match_id),
        "map_asset": map_asset,
        "wasm_size": wasm_size,
        "map_size": map_size,
    })


@app.get("/matches/{match_id}/replay3d/{fname}")
def match_replay3d_artifact(request: Request, match_id: int, fname: str):
    """Auth-gated artifact serving: manifest.json + rNN.3dr only."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    if db.get_match(match_id, user["steamid64"]) is None:
        raise HTTPException(status_code=404)
    path = replay3d.artifact_path(match_id, fname)
    if path is None:
        raise HTTPException(status_code=404)
    media = "application/json" if fname.endswith(".json") else "application/octet-stream"
    return FileResponse(str(path), media_type=media)


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
