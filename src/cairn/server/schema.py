"""Server-side schema: devices, visibility grants, and the aggregated catalogue.

This is a *different* database from the agent's index, and deliberately so. The
agent's index is scan state for one machine -- what it saw, when, and what changed.
The server's catalogue is the union across devices, scoped by who may see what.

Visibility is explicit and additive. A device always sees itself; anything more
requires a row in `device_grants`. There is no implicit "admin sees all", because
the failure mode of getting that wrong is one household member reading another's
medical records.
"""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    token        TEXT    NOT NULL UNIQUE,
    platform     TEXT,
    enrolled_ts  INTEGER NOT NULL,
    last_seen_ts INTEGER
);

-- viewer_id may read catalogue rows and objects belonging to visible_id.
CREATE TABLE IF NOT EXISTS device_grants (
    viewer_id  INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    visible_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    granted_ts INTEGER NOT NULL,
    PRIMARY KEY (viewer_id, visible_id)
);

CREATE TABLE IF NOT EXISTS catalogue (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    path_key    TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    ext         TEXT,
    sha256      TEXT,
    category    TEXT,
    confidence  REAL,
    score       REAL,
    state       TEXT    NOT NULL,
    updated_ts  INTEGER NOT NULL,
    UNIQUE (device_id, path_key)
);

CREATE INDEX IF NOT EXISTS idx_cat_device ON catalogue(device_id);
CREATE INDEX IF NOT EXISTS idx_cat_sha    ON catalogue(sha256);
CREATE INDEX IF NOT EXISTS idx_cat_score  ON catalogue(score DESC);

CREATE TABLE IF NOT EXISTS objects (
    sha256    TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    refcount  INTEGER NOT NULL DEFAULT 0,
    stored_ts INTEGER NOT NULL
);

-- Which device uploaded which object. Also the ACL for downloads: you may fetch
-- an object only if a device you can see has a reference to it.
CREATE TABLE IF NOT EXISTS object_refs (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    sha256    TEXT    NOT NULL REFERENCES objects(sha256),
    added_ts  INTEGER NOT NULL,
    PRIMARY KEY (device_id, sha256)
);

CREATE INDEX IF NOT EXISTS idx_refs_sha ON object_refs(sha256);

CREATE VIRTUAL TABLE IF NOT EXISTS catalogue_search USING fts5(name, path, body);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key,value) VALUES ('schema_version',?)", (str(SCHEMA_VERSION),))
    elif int(row["value"]) != SCHEMA_VERSION:
        raise RuntimeError(f"server db is schema v{row['value']}, this build expects v{SCHEMA_VERSION}")

    return conn


def enrollment_secret(conn: sqlite3.Connection) -> str:
    """The one-time secret a device must present to enroll.

    Generated on first run and printed at startup. Without it, anything that can
    reach the port could add itself as a device -- which behind a tunnel means
    anything that can reach loopback on the server.
    """
    row = conn.execute("SELECT value FROM meta WHERE key='enroll_secret'").fetchone()
    if row:
        return row["value"]
    secret = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO meta(key,value) VALUES ('enroll_secret',?)", (secret,))
    return secret


def visible_device_ids(conn: sqlite3.Connection, device_id: int) -> list[int]:
    """Devices this one may read: itself, plus anything explicitly granted."""
    rows = conn.execute(
        "SELECT visible_id FROM device_grants WHERE viewer_id = ?", (device_id,)
    ).fetchall()
    return [device_id] + [r["visible_id"] for r in rows]
