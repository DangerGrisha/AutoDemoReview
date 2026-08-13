"""SQLite data access: connection factory, schema (v1), and CRUD.

Matches are many-to-many with users via `match_participants` (one row per real
player, registered or not). Visibility = participation OR upload. Schema
versioning + migration live in web/migrations.py, invoked from init_schema().
"""

import sqlite3

from . import config

# --- v1 schema (fresh installs). Existing DBs are migrated in web/migrations.py,
# which reuses PARTICIPANTS_DDL and INDEXES_DDL (kept as separate constants so the
# migration can create the participants table BEFORE dedup and the unique-hash
# index AFTER it). ---
_USERS_DDL = """
CREATE TABLE IF NOT EXISTS users (
    steamid64     TEXT PRIMARY KEY,
    persona_name  TEXT NOT NULL,
    avatar_url    TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_MATCHES_DDL = """
CREATE TABLE IF NOT EXISTS matches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    uploader_steamid  TEXT NOT NULL REFERENCES users(steamid64) ON DELETE CASCADE,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    original_filename TEXT,
    map_name          TEXT,
    my_score          INTEGER NOT NULL,   -- uploader-relative fallback (non-player uploader)
    opp_score         INTEGER NOT NULL,
    won               INTEGER NOT NULL,
    n_rounds          INTEGER NOT NULL,
    ref_name          TEXT,
    ref_sid           TEXT,
    ref_kills         INTEGER NOT NULL,
    ref_deaths        INTEGER NOT NULL,
    found             INTEGER NOT NULL,
    dem_sha256        TEXT,
    data_json         TEXT NOT NULL
);
"""

PARTICIPANTS_DDL = """
CREATE TABLE IF NOT EXISTS match_participants (
    match_id      INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    steamid64     TEXT    NOT NULL,          -- NOT an FK: unregistered players get rows too
    name          TEXT,
    won           INTEGER NOT NULL,          -- perspective-corrected for this player
    my_score      INTEGER NOT NULL,
    opp_score     INTEGER NOT NULL,
    rounds_played INTEGER NOT NULL,
    kills         INTEGER NOT NULL,
    deaths        INTEGER NOT NULL,
    assists       INTEGER NOT NULL,
    hs            INTEGER NOT NULL,
    adr           REAL NOT NULL,
    kast          REAL NOT NULL,
    rating        REAL NOT NULL,
    hs_pct        REAL NOT NULL,
    opening_k     INTEGER NOT NULL,
    opening_d     INTEGER NOT NULL,
    trade_kills   INTEGER NOT NULL,
    clutch        INTEGER NOT NULL,
    clutch_won    INTEGER NOT NULL,
    flash_assists INTEGER NOT NULL,
    multi_json    TEXT,
    weapons_json  TEXT,
    h2h_json      TEXT,
    PRIMARY KEY (match_id, steamid64)
);
"""

INDEXES_DDL = """
CREATE INDEX IF NOT EXISTS idx_matches_uploader ON matches(uploader_steamid, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_matches_hash ON matches(dem_sha256) WHERE dem_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_participants_sid ON match_participants(steamid64);
"""

# --- v2: Steam-bot auto-import subscriptions (one row per opted-in player) ---
#
# `match_auth_code` is the player's CS2 match-history authentication code: a
# scoped, revocable token that only authorises reading their match-sharing
# history -- NOT a Steam password. It still must never be logged or shown back in
# full (see steambot/README.md). `last_sharecode` is the walking cursor: the most
# recent match we have already imported; GetNextMatchSharingCode walks forward
# from it. `chat_optin` + `friend_state` govern Steam-chat delivery, which needs
# the bot to be a friend -- the user adds the bot, the bot accepts (never the
# other way round: outgoing bot invites are a spam-flag trigger, and Steam caps
# friends at 250 anyway, so chat is a bonus channel, not the only one).
STEAM_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS steam_links (
    steamid64       TEXT PRIMARY KEY REFERENCES users(steamid64) ON DELETE CASCADE,
    match_auth_code TEXT NOT NULL,
    last_sharecode  TEXT NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    chat_optin      INTEGER NOT NULL DEFAULT 1,
    friend_state    TEXT    NOT NULL DEFAULT 'none',   -- none | pending | friends
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_checked_at TEXT,
    last_match_at   TEXT,
    matches_found   INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    -- A match is detected minutes before Valve publishes its demo, so the found
    -- sharecode is parked here while we wait. Waiting is NOT a failure (it must
    -- not trip fail_count), but it can't be endless either: Valve prunes demos
    -- after ~1-2 weeks, and a match whose demo never appears would block the
    -- cursor forever -- hence a try counter the worker uses to give up and skip.
    pending_sharecode TEXT,
    pending_tries     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_steam_links_due
    ON steam_links(enabled, last_checked_at);
"""

SCHEMA = (_USERS_DDL + _MATCHES_DDL + PARTICIPANTS_DDL + INDEXES_DDL
          + STEAM_LINKS_DDL)

# Columns of match_participants that extract_participants() supplies (match_id added at insert).
_PART_COLS = (
    "steamid64", "name", "won", "my_score", "opp_score", "rounds_played",
    "kills", "deaths", "assists", "hs", "adr", "kast", "rating", "hs_pct",
    "opening_k", "opening_d", "trade_kills", "clutch", "clutch_won",
    "flash_assists", "multi_json", "weapons_json", "h2h_json",
)


def connect():
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema():
    from . import migrations
    conn = connect()
    try:
        migrations.run(conn, SCHEMA)
    finally:
        conn.close()


# ---- users ----

def upsert_user(steamid64, persona_name, avatar_url):
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO users (steamid64, persona_name, avatar_url, last_login_at)
                    VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(steamid64) DO UPDATE SET
                    persona_name = excluded.persona_name,
                    avatar_url   = excluded.avatar_url,
                    last_login_at = datetime('now')""",
            (steamid64, persona_name, avatar_url),
        )
        conn.commit()
    finally:
        conn.close()


def get_user(steamid64):
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM users WHERE steamid64 = ?", (steamid64,)
        ).fetchone()
    finally:
        conn.close()


# ---- matches ----

def find_match_by_hash(dem_sha256):
    """Global dedup: return the match id for this demo hash, or None."""
    if not dem_sha256:
        return None
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id FROM matches WHERE dem_sha256 = ?", (dem_sha256,)
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def insert_match_with_participants(*, match, participants):
    """Insert a match and its participant rows atomically. Returns the match id.

    `match` is a dict of the matches columns (minus id/created_at); `participants`
    is a list of dicts from participants.extract_participants(). Raises
    sqlite3.IntegrityError if the demo hash already exists (global unique).
    """
    conn = connect()
    try:
        cur = conn.execute(
            """INSERT INTO matches
                 (uploader_steamid, original_filename, map_name, my_score, opp_score,
                  won, n_rounds, ref_name, ref_sid, ref_kills, ref_deaths, found,
                  dem_sha256, data_json)
               VALUES (:uploader_steamid, :original_filename, :map_name, :my_score,
                  :opp_score, :won, :n_rounds, :ref_name, :ref_sid, :ref_kills,
                  :ref_deaths, :found, :dem_sha256, :data_json)""",
            match,
        )
        match_id = cur.lastrowid
        _insert_participants(conn, match_id, participants)
        conn.commit()
        return match_id
    finally:
        conn.close()


def _insert_participants(conn, match_id, participants):
    if not participants:
        return
    cols = ("match_id",) + _PART_COLS
    placeholders = ", ".join(":" + c for c in cols)
    sql = f"INSERT OR IGNORE INTO match_participants ({', '.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [{"match_id": match_id, **p} for p in participants])


def can_access(match_id, steamid):
    """True if `steamid` may view match_id (participant OR uploader)."""
    conn = connect()
    try:
        row = conn.execute(
            """SELECT 1 FROM matches m WHERE m.id = :id AND (m.uploader_steamid = :sid
                 OR EXISTS(SELECT 1 FROM match_participants p
                           WHERE p.match_id = m.id AND p.steamid64 = :sid))""",
            {"id": match_id, "sid": steamid},
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_match(match_id, steamid):
    """Access-scoped fetch (participant or uploader). None if not visible."""
    conn = connect()
    try:
        return conn.execute(
            """SELECT m.* FROM matches m WHERE m.id = :id AND (m.uploader_steamid = :sid
                 OR EXISTS(SELECT 1 FROM match_participants p
                           WHERE p.match_id = m.id AND p.steamid64 = :sid))""",
            {"id": match_id, "sid": steamid},
        ).fetchone()
    finally:
        conn.close()


def list_matches(steamid):
    """The viewer's matches (participated in OR uploaded), with THEIR own stats."""
    conn = connect()
    try:
        return conn.execute(
            """SELECT m.id, m.created_at, m.map_name, m.original_filename,
                      m.uploader_steamid, u.persona_name AS uploader_name,
                      COALESCE(p.won,       m.won)        AS won,
                      COALESCE(p.my_score,  m.my_score)   AS my_score,
                      COALESCE(p.opp_score, m.opp_score)  AS opp_score,
                      COALESCE(p.kills,     m.ref_kills)  AS kills,
                      COALESCE(p.deaths,    m.ref_deaths) AS deaths,
                      (p.steamid64 IS NOT NULL)           AS is_participant
                 FROM matches m
                 LEFT JOIN match_participants p
                        ON p.match_id = m.id AND p.steamid64 = :sid
                 LEFT JOIN users u ON u.steamid64 = m.uploader_steamid
                WHERE p.steamid64 = :sid OR m.uploader_steamid = :sid
                ORDER BY m.created_at DESC, m.id DESC""",
            {"sid": steamid},
        ).fetchall()
    finally:
        conn.close()


# ---- career ----

def career_totals(steamid):
    """Aggregate scalar row over all of the viewer's participant rows (or None)."""
    conn = connect()
    try:
        return conn.execute(
            """SELECT COUNT(*)                              AS matches,
                      COALESCE(SUM(won), 0)                 AS wins,
                      COALESCE(SUM(1 - won), 0)             AS losses,
                      COALESCE(SUM(rounds_played), 0)       AS rounds,
                      COALESCE(SUM(kills), 0)               AS kills,
                      COALESCE(SUM(deaths), 0)              AS deaths,
                      COALESCE(SUM(assists), 0)             AS assists,
                      COALESCE(SUM(hs), 0)                  AS hs,
                      COALESCE(SUM(adr * rounds_played), 0)    AS adr_w,
                      COALESCE(SUM(kast * rounds_played), 0)   AS kast_w,
                      COALESCE(SUM(rating * rounds_played), 0) AS rating_w
                 FROM match_participants WHERE steamid64 = ?""",
            (steamid,),
        ).fetchone()
    finally:
        conn.close()


def career_blobs(steamid):
    """The viewer's per-match weapons_json + h2h_json rows (for career merge)."""
    conn = connect()
    try:
        return conn.execute(
            "SELECT weapons_json, h2h_json FROM match_participants WHERE steamid64 = ?",
            (steamid,),
        ).fetchall()
    finally:
        conn.close()


# ---- steam-bot subscriptions (auto-import of finished matches) ----

def upsert_steam_link(steamid, match_auth_code, last_sharecode):
    """Create or update a subscription, clearing any previous failure state.

    Re-submitting the form is how a user fixes a revoked auth code, so the error
    counters reset here -- otherwise a subscription paused by repeated failures
    would stay paused after being fixed.
    """
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO steam_links (steamid64, match_auth_code, last_sharecode)
                    VALUES (?, ?, ?)
               ON CONFLICT(steamid64) DO UPDATE SET
                    match_auth_code = excluded.match_auth_code,
                    last_sharecode  = excluded.last_sharecode,
                    enabled         = 1,
                    fail_count      = 0,
                    last_error      = NULL""",
            (steamid, match_auth_code, last_sharecode),
        )
        conn.commit()
    finally:
        conn.close()


def get_steam_link(steamid):
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM steam_links WHERE steamid64 = ?", (steamid,)).fetchone()
    finally:
        conn.close()


def delete_steam_link(steamid):
    conn = connect()
    try:
        conn.execute("DELETE FROM steam_links WHERE steamid64 = ?", (steamid,))
        conn.commit()
    finally:
        conn.close()


def set_steam_link_flags(steamid, *, enabled=None, chat_optin=None, friend_state=None):
    """Patch the toggles/state of one subscription (only the given fields)."""
    sets, args = [], []
    for col, val in (("enabled", enabled), ("chat_optin", chat_optin)):
        if val is not None:
            sets.append(f"{col} = ?")
            args.append(1 if val else 0)
    if friend_state is not None:
        sets.append("friend_state = ?")
        args.append(friend_state)
    if not sets:
        return
    args.append(steamid)
    conn = connect()
    try:
        conn.execute(f"UPDATE steam_links SET {', '.join(sets)} WHERE steamid64 = ?", args)
        conn.commit()
    finally:
        conn.close()


def list_due_steam_links(max_age_s, limit=50, max_fails=10, pending_age_s=None):
    """Enabled subscriptions due for a poll, oldest first.

    Two cadences: a subscription with a match already detected but no demo yet
    (`pending_sharecode`) is due after `pending_age_s`, everyone else after
    `max_age_s`. Waiting for a demo only costs a Game Coordinator lookup, so it
    can be checked often; the idle poll hits Steam's rate-limited Web API and a
    new match cannot appear faster than it is played.

    `max_fails` parks a subscription that keeps failing (revoked auth code, say)
    instead of hammering the API with it forever -- re-submitting the form
    resets the counter and revives it.
    """
    if pending_age_s is None:
        pending_age_s = max_age_s
    conn = connect()
    try:
        return conn.execute(
            """SELECT * FROM steam_links
                WHERE enabled = 1 AND fail_count < :max_fails
                  AND (last_checked_at IS NULL
                       OR (pending_sharecode IS NOT NULL
                           AND last_checked_at <= datetime('now', :pending_cutoff))
                       OR (pending_sharecode IS NULL
                           AND last_checked_at <= datetime('now', :cutoff)))
                ORDER BY last_checked_at IS NOT NULL, last_checked_at
                LIMIT :limit""",
            {"max_fails": max_fails,
             "cutoff": f"-{int(max_age_s)} seconds",
             "pending_cutoff": f"-{int(pending_age_s)} seconds",
             "limit": limit},
        ).fetchall()
    finally:
        conn.close()


def steam_link_checked(steamid, *, error=None):
    """Stamp a poll attempt. `error=None` clears the failure streak."""
    conn = connect()
    try:
        if error is None:
            conn.execute(
                """UPDATE steam_links SET last_checked_at = datetime('now'),
                        fail_count = 0, last_error = NULL WHERE steamid64 = ?""",
                (steamid,))
        else:
            conn.execute(
                """UPDATE steam_links SET last_checked_at = datetime('now'),
                        fail_count = fail_count + 1, last_error = ?
                    WHERE steamid64 = ?""",
                (str(error)[:500], steamid))
        conn.commit()
    finally:
        conn.close()


def steam_link_advance(steamid, sharecode):
    """Move the cursor to an imported match, count it, and clear the pending slot."""
    conn = connect()
    try:
        conn.execute(
            """UPDATE steam_links SET last_sharecode = ?, last_match_at = datetime('now'),
                    matches_found = matches_found + 1,
                    pending_sharecode = NULL, pending_tries = 0
                WHERE steamid64 = ?""",
            (sharecode, steamid))
        conn.commit()
    finally:
        conn.close()


def steam_link_skip_pending(steamid, sharecode):
    """Give up on a match whose demo never appeared: move the cursor past it.

    Without this the cursor would sit on a match Valve has already pruned and no
    later match would ever be imported.
    """
    conn = connect()
    try:
        conn.execute(
            """UPDATE steam_links SET last_sharecode = ?, pending_sharecode = NULL,
                    pending_tries = 0 WHERE steamid64 = ?""",
            (sharecode, steamid))
        conn.commit()
    finally:
        conn.close()


def steam_link_pending(steamid, sharecode):
    """Record that `sharecode` is detected but still waiting for its demo."""
    conn = connect()
    try:
        conn.execute(
            """UPDATE steam_links
                  SET pending_sharecode = :code,
                      pending_tries = CASE WHEN pending_sharecode = :code
                                           THEN pending_tries + 1 ELSE 1 END
                WHERE steamid64 = :sid""",
            {"code": sharecode, "sid": steamid})
        conn.commit()
    finally:
        conn.close()


def steam_links_awaiting_friend():
    """SteamIDs allowed to become the bot's friend: opted into chat, not yet friends.

    The bot accepts invites ONLY from these -- otherwise it would collect random
    people and burn through Steam's 250-friend cap.
    """
    conn = connect()
    try:
        return [r["steamid64"] for r in conn.execute(
            """SELECT steamid64 FROM steam_links
                WHERE chat_optin = 1 AND friend_state != 'friends'""").fetchall()]
    finally:
        conn.close()
