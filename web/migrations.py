"""Schema versioning + migrations, invoked from db.init_schema().

  user_version 0 = pre-sharing single-owner schema (matches.user_steamid).
  user_version 1 = many-to-many sharing (uploader_steamid + match_participants).

The v0->v1 backfill re-reads each match's stored data_json, so it lives here (not
in pure SQL). It reuses participants.extract_participants — the same transform the
upload path uses — so backfilled and freshly-inserted rows are identical.

We drive the connection in autocommit mode (isolation_level=None) and control the
transaction with explicit BEGIN/COMMIT, executing each DDL statement individually.
This avoids sqlite3.executescript(), which issues an implicit COMMIT and would
otherwise break the migration's all-or-nothing transaction.
"""

import json

from . import db, participants


def _user_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


def _has_column(conn, table, col):
    return any(r["name"] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _exec_each(conn, ddl):
    """Run a multi-statement DDL string one statement at a time (no implicit commit)."""
    for stmt in ddl.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)


CURRENT_VERSION = 3


def run(conn, full_schema):
    """Bring `conn` up to the current schema version (idempotent)."""
    conn.isolation_level = None                       # explicit transaction control
    if _user_version(conn) >= CURRENT_VERSION:
        return
    if not _table_exists(conn, "matches"):
        _exec_each(conn, full_schema)                 # fresh install: already current
        conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
        return
    if _user_version(conn) < 1:
        _migrate_v0_to_v1(conn)
    if _user_version(conn) < 2:
        _migrate_v1_to_v2(conn)
    _migrate_v2_to_v3(conn)


def _migrate_v1_to_v2(conn):
    """Add the Steam-bot subscription table. Pure addition -- no data to move."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _exec_each(conn, db.STEAM_LINKS_DDL)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("PRAGMA user_version = 2")


def _migrate_v2_to_v3(conn):
    """Park a detected-but-not-yet-published match on the subscription row.

    A DB created at v2 already has the columns (they live in STEAM_LINKS_DDL), so
    each ADD COLUMN is guarded -- SQLite has no ADD COLUMN IF NOT EXISTS.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        _exec_each(conn, db.STEAM_LINKS_DDL)          # no-op if it already exists
        if not _has_column(conn, "steam_links", "pending_sharecode"):
            conn.execute("ALTER TABLE steam_links ADD COLUMN pending_sharecode TEXT")
        if not _has_column(conn, "steam_links", "pending_tries"):
            conn.execute("ALTER TABLE steam_links ADD COLUMN pending_tries "
                         "INTEGER NOT NULL DEFAULT 0")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("PRAGMA user_version = 3")


def _migrate_v0_to_v1(conn):
    """Single-owner -> many-to-many, preserving existing matches. Idempotent: each
    step is guarded / IF NOT EXISTS / INSERT OR IGNORE, so a crash before the
    version bump converges on re-run."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. rename the owner column (guard makes re-runs safe).
        if _has_column(conn, "matches", "user_steamid"):
            conn.execute(
                "ALTER TABLE matches RENAME COLUMN user_steamid TO uploader_steamid")

        # 2. create the participants table (before dedup / unique index).
        _exec_each(conn, db.PARTICIPANTS_DDL)

        # 3. backfill participants from every match's stored data_json.
        for row in conn.execute("SELECT id, data_json FROM matches").fetchall():
            try:
                summary = (json.loads(row["data_json"]) or {}).get("summary") or {}
            except (ValueError, TypeError):
                continue
            db._insert_participants(conn, row["id"], participants.extract_participants(summary))

        # 4. dedupe duplicate demos (same hash from old per-user dedup) BEFORE the
        #    unique index — keep one per hash (prefer found=1, then lowest id).
        for d in conn.execute(
            """SELECT dem_sha256 FROM matches
                WHERE dem_sha256 IS NOT NULL
                GROUP BY dem_sha256 HAVING COUNT(*) > 1""").fetchall():
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM matches WHERE dem_sha256 = ? ORDER BY found DESC, id ASC",
                (d["dem_sha256"],)).fetchall()]
            for extra in ids[1:]:
                conn.execute("DELETE FROM matches WHERE id = ?", (extra,))   # CASCADE

        # 5. rebuild indexes (old ones dropped; unique-hash now safe post-dedup).
        conn.execute("DROP INDEX IF EXISTS idx_matches_user")
        conn.execute("DROP INDEX IF EXISTS idx_matches_user_hash")
        _exec_each(conn, db.INDEXES_DDL)

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    conn.execute("PRAGMA user_version = 1")
