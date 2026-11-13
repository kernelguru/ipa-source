"""SQLite storage. One short-lived connection per operation; WAL for concurrency."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_write_lock = threading.RLock()

SCHEMA = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS api_tokens (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT 'full',      -- full | upload | read
        prefix TEXT NOT NULL,
        token_hash TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    CREATE TABLE IF NOT EXISTS apps (
        id INTEGER PRIMARY KEY,
        bundle_id TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        subtitle TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        developer TEXT NOT NULL DEFAULT '',
        tint TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT 'other',
        icon_url TEXT NOT NULL DEFAULT '',        -- external override
        has_icon INTEGER NOT NULL DEFAULT 0,      -- icon.png in storage
        screenshots TEXT NOT NULL DEFAULT '[]',
        entitlements TEXT NOT NULL DEFAULT '[]',  -- extra entitlements (merged)
        privacy TEXT NOT NULL DEFAULT '{}',       -- extra usage descriptions (merged)
        visible INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT 'github',
        repo TEXT NOT NULL,
        use_releases INTEGER NOT NULL DEFAULT 1,
        use_artifacts INTEGER NOT NULL DEFAULT 0,
        include_prereleases INTEGER NOT NULL DEFAULT 0,
        asset_pattern TEXT NOT NULL DEFAULT '*.ipa',
        artifact_name TEXT NOT NULL DEFAULT '*',
        artifact_branch TEXT NOT NULL DEFAULT '',
        keep_releases INTEGER NOT NULL DEFAULT 5,
        keep_artifacts INTEGER NOT NULL DEFAULT 3,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_sync_at TEXT,
        last_status TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(kind, repo)
    );
    CREATE TABLE IF NOT EXISTS builds (
        id INTEGER PRIMARY KEY,
        app_id INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
        source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
        origin TEXT NOT NULL,                     -- release | artifact | upload | webdav
        origin_ref TEXT NOT NULL,                 -- e.g. tag, artifact id, upload uuid
        version TEXT NOT NULL,
        build_version TEXT NOT NULL,
        min_os TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        date TEXT NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        filename TEXT NOT NULL,
        path TEXT NOT NULL,                       -- relative to STORAGE_DIR
        permissions TEXT NOT NULL DEFAULT '{}',
        visible INTEGER NOT NULL DEFAULT 1,
        pinned INTEGER NOT NULL DEFAULT 0,        -- never removed by retention
        created_at TEXT NOT NULL,
        UNIQUE(source_id, origin, origin_ref)
    );
    CREATE INDEX IF NOT EXISTS builds_app ON builds(app_id, date);
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        level TEXT NOT NULL,
        message TEXT NOT NULL
    );
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def conn():
    c = _connect()
    try:
        yield c
    finally:
        c.close()


@contextmanager
def tx():
    """Serialized write transaction."""
    with _write_lock:
        c = _connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()


def init():
    config.ensure_dirs()
    with _write_lock:
        c = _connect()
        try:
            c.execute("PRAGMA journal_mode = WAL")
            ver = c.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(SCHEMA[ver:], start=ver + 1):
                c.executescript(script)
                c.execute(f"PRAGMA user_version = {i}")
        finally:
            c.close()


def one(sql, *args):
    with conn() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None


def all_(sql, *args):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def execute(sql, *args):
    with tx() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


# ---------------------------------------------------------------- settings --
DEFAULT_SETTINGS = {
    "base_url": "",
    "feed_name": "My Apps",
    "feed_identifier": "local.ipa-source",
    "feed_subtitle": "",
    "feed_description": "",
    "feed_icon_url": "",
    "feed_header_url": "",
    "feed_website": "",
    "feed_tint": "#5C7AEA",
    "feed_private": "0",          # require feed key in URL
    "feed_key": "",
    "developer": "",
    "github_token": "",
    "github_user": "",
    "github_client_id": "",       # OAuth App client id for device flow
    "sync_interval": "900",
    "webhook_secret": "",
    "dedupe_prefer": "release",   # which origin wins on identical version+build
}


def get_setting(key: str) -> str:
    r = one("SELECT value FROM settings WHERE key = ?", key)
    if r is None or r["value"] is None:
        return DEFAULT_SETTINGS.get(key, "")
    return r["value"]


def get_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    for r in all_("SELECT key, value FROM settings"):
        out[r["key"]] = r["value"]
    return out


def set_settings(values: dict):
    with tx() as c:
        for k, v in values.items():
            c.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, "" if v is None else str(v)),
            )


# ------------------------------------------------------------------ events --
def log_event(level: str, message: str):
    print(f"[{now()}] {level.upper():5} {message}", flush=True)
    try:
        with tx() as c:
            c.execute("INSERT INTO events(ts, level, message) VALUES(?,?,?)", (now(), level, message))
            c.execute("DELETE FROM events WHERE id <= (SELECT MAX(id) - 1000 FROM events)")
    except Exception:
        pass


def jloads(s, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default
