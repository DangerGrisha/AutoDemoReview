"""World of Tanks Blitz pages: battle list with drag & drop upload, battle report.

Separate from the CS2 flow on purpose -- own router, own table, own renderer
(src/blitzreview). The two games share the Steam login and the site chrome,
nothing else, until the UI merge.

Replays are small (0.5-5 MB) and parse in well under a second, so they are
parsed inline in the request, like CS2 demos, without the parse semaphore. The
drop zone sends one file per request (per-file progress, small requests); the
no-JS form may send many at once and gets the same per-file results rendered.
"""

import hashlib
import html
import json
import logging
import sqlite3
import sys
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from blitzreview import battle as blitz_battle  # noqa: E402
from blitzreview import career as blitz_career  # noqa: E402
from blitzreview import gamedata  # noqa: E402
from blitzreview.container import MAX_FILE_BYTES, ReplayError  # noqa: E402
from blitzreview.render import render_battle_html  # noqa: E402

log = logging.getLogger("web.blitz")

router = APIRouter(prefix="/blitz")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def _pct(value):
    return "—" if value is None else f"{round(100 * value)}%"


def _num(value, digits=0):
    if value is None:
        return "—"
    if digits:
        return f"{value:.{digits}f}".replace(".", ",")
    return f"{int(round(value)):,}".replace(",", " ")


def _plu(n, one, few, many):
    """Russian noun agreement: 1 победа, 2 победы, 5 побед."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


templates.env.filters["pct"] = _pct
templates.env.filters["num"] = _num
templates.env.globals["plu"] = _plu
templates.env.globals["class_name"] = gamedata.class_name
templates.env.globals["tier_roman"] = gamedata.tier_roman

MAX_FILES_PER_REQUEST = 50
REPLAY_SUFFIX = ".wotbreplay"


def _user(request):
    sid = request.session.get("steamid")
    return db.get_user(sid) if sid else None


def _row(battle, *, uploader, filename, sha256):
    """The flat wotb_battles columns for one parsed battle."""
    me = next((v for v in battle["vehicles"] if v["is_author"]), None)
    stats = (me or {}).get("stats") or {}
    tank = battle["author"]["tank"]
    return {
        "uploader_steamid": uploader,
        "original_filename": filename,
        "replay_sha256": sha256,
        "arena_id": battle.get("arena_id"),
        "recorder_account": battle["author"].get("account_id"),
        "recorder_name": battle["author"].get("nickname"),
        "started_at": battle.get("started_at"),
        "map_key": battle["map"].get("key"),
        "map_name": battle["map"].get("name"),
        "mode_name": (battle.get("mode") or {}).get("name"),
        "tank_name": tank.get("name"),
        "tank_tier": tank.get("tier"),
        "tank_type": tank.get("type"),
        "outcome": battle["outcome"],
        "damage": stats.get("damage"),
        "kills": stats.get("kills"),
        "survived": None if not me or me["alive"] is None else int(bool(me["alive"])),
        "has_results": int(bool(battle["has_results"])),
        "data_json": json.dumps(battle, ensure_ascii=False, separators=(",", ":")),
    }


def ingest(data, *, uploader, filename):
    """Parse and store one replay. Returns a per-file result dict for the UI.

    status: "new" | "duplicate" (already stored) | "updated" (the stored copy
    of the battle had no battle results and this one does, or it was parsed by
    an older version and lacks what the report now shows, e.g. the 2D replay).
    """
    result = {"name": filename, "ok": False}
    if not filename.lower().endswith(REPLAY_SUFFIX):
        result["error"] = "нужен файл .wotbreplay"
        return result
    if len(data) > MAX_FILE_BYTES:
        result["error"] = f"файл больше {MAX_FILE_BYTES // (1024 * 1024)} МБ"
        return result
    sha256 = hashlib.sha256(data).hexdigest()

    existing = db.find_wotb_battle(uploader, sha256=sha256)
    if existing and existing["format"] >= blitz_battle.FORMAT:
        return {**result, "ok": True, "status": "duplicate", "id": existing["id"]}

    try:
        battle = blitz_battle.load(data)
    except ReplayError as exc:
        result["error"] = f"не похоже на реплей Blitz ({exc})"
        return result
    except Exception as exc:  # noqa: BLE001 -- a parser bug must not 500 the batch
        result["error"] = f"не удалось разобрать реплей ({type(exc).__name__})"
        return result

    row = _row(battle, uploader=uploader, filename=filename, sha256=sha256)
    same = existing or db.find_wotb_battle(uploader, arena_id=row["arena_id"],
                                           recorder_account=row["recorder_account"])
    if same:
        gains_results = row["has_results"] and not same["has_results"]
        outdated = same["format"] < blitz_battle.FORMAT
        # Never trade the battle results for anything: a thinner copy of an
        # outdated battle still leaves it alone.
        if gains_results or (outdated and row["has_results"] >= same["has_results"]):
            db.replace_wotb_battle(same["id"], row)
            return {**result, "ok": True, "status": "updated", "id": same["id"]}
        return {**result, "ok": True, "status": "duplicate", "id": same["id"]}
    try:
        battle_id = db.insert_wotb_battle(row)
    except sqlite3.IntegrityError:  # a parallel upload of the same file won the race
        again = db.find_wotb_battle(uploader, sha256=sha256, arena_id=row["arena_id"],
                                    recorder_account=row["recorder_account"])
        if again is None:
            raise
        return {**result, "ok": True, "status": "duplicate", "id": again["id"]}
    return {**result, "ok": True, "status": "new", "id": battle_id}


OUTCOME_LABELS = {"win": "Победа", "loss": "Поражение", "draw": "Ничья", "unknown": "?"}


def _list_page(request, user, results=None, status_code=200):
    battles = []
    for row in db.list_wotb_battles(user["steamid64"]):
        kind = gamedata.class_name(row["tank_type"]) or ""
        battles.append(dict(row,
                            tank_kind=f"{gamedata.tier_roman(row['tank_tier'])} {kind}".strip(),
                            outcome_label=OUTCOME_LABELS.get(row["outcome"], "?")))
    return templates.TemplateResponse(
        request, "blitz.html",
        {"user": user, "battles": battles, "results": results,
         "max_mb": MAX_FILE_BYTES // (1024 * 1024)},
        status_code=status_code)


@router.get("", response_class=HTMLResponse)
def blitz_home(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return _list_page(request, user)


@router.post("/upload")
def blitz_upload(request: Request, replays: list[UploadFile] = File(...)):
    user = _user(request)
    wants_json = "application/json" in request.headers.get("accept", "")
    if not user:
        if wants_json:
            return JSONResponse({"error": "login required"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    results = []
    for upload in replays[:MAX_FILES_PER_REQUEST]:
        name = Path(upload.filename or "replay").name
        data = upload.file.read(MAX_FILE_BYTES + 1)
        results.append(ingest(data, uploader=user["steamid64"], filename=name))
    for upload in replays[MAX_FILES_PER_REQUEST:]:
        results.append({"name": Path(upload.filename or "replay").name, "ok": False,
                        "error": f"за один раз — не больше {MAX_FILES_PER_REQUEST} файлов"})

    if wants_json:
        return JSONResponse({"results": results})
    return _list_page(request, user, results=results)


@router.get("/career", response_class=HTMLResponse)
def blitz_career_view(request: Request, account: int | None = None):
    """One recorder's battles together: per tank and their habits (the coach's
    rules that keep firing). Replays of several accounts never mix -- the
    uploader picks whose; by default the one with the most battles."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    recorders = [dict(r) for r in db.wotb_recorders(user["steamid64"])]
    chosen = next((r for r in recorders if r["recorder_account"] == account), None)
    chosen = chosen or (recorders[0] if recorders else None)
    lines = []
    if chosen:
        for row in db.list_wotb_battle_data(user["steamid64"], chosen["recorder_account"]):
            try:
                lines.append(blitz_career.battle_line(row["id"], json.loads(row["data_json"])))
            except Exception:  # noqa: BLE001 -- one odd battle must not hide the career
                log.exception("career line failed for wotb battle %s", row["id"])
    return templates.TemplateResponse(
        request, "blitz_career.html",
        {"user": user, "recorders": recorders, "chosen": chosen,
         "career": blitz_career.summarize(lines)})


@router.get("/battles/{battle_id}", response_class=HTMLResponse)
def blitz_battle_view(request: Request, battle_id: int):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    row = db.get_wotb_battle(battle_id, user["steamid64"])
    if row is None:
        raise HTTPException(status_code=404)
    battle = json.loads(row["data_json"])
    avatar = (f'<img src="{html.escape(user["avatar_url"])}" alt="">'
              if user["avatar_url"] else "")
    site_nav = ('<nav class="site-nav"><a href="/blitz">&#8592; Мои бои Blitz</a>'
                '<a href="/blitz/career">Карьера</a>'
                '<span class="sn-brand">demoReview</span>'
                f'<span class="sn-user">{avatar}{html.escape(user["persona_name"])}</span></nav>')
    return HTMLResponse(render_battle_html(battle, site_nav=site_nav))
