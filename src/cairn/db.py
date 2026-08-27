"""Schema and connection handling.

Design notes worth keeping:

- `search` is a standalone FTS5 table whose rowid is deliberately kept equal to
  `files.id`. No triggers: re-scan deletes the row and re-inserts it. Triggers
  on external-content FTS tables are a well-known source of silent index drift,
  and this codebase would rather do the sync in one visible place.

- `objects.refcount` exists before anything needs it. In Phase 1 a file maps to
  exactly one object; in Phase 3 it maps to many chunks. Reference counting is
  the seam garbage collection plugs into, and putting it in now means the
  schema does not have to be rewritten around it later.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL,
    path_key      TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    size          INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    atime_ns      INTEGER,
    ext           TEXT,
    sha256        TEXT,
    category      TEXT,
    confidence    REAL,
    score         REAL,
    state         TEXT    NOT NULL,
    skip_reason   TEXT,
    matched_rules TEXT,
    first_seen_ts INTEGER NOT NULL,
    last_scan_ts  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_sha      ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_score    ON files(score DESC);
CREATE INDEX IF NOT EXISTS idx_files_state    ON files(state);

CREATE TABLE IF NOT EXISTS objects (
    sha256    TEXT PRIMARY KEY,
    size      INTEGER NOT NULL,
    refcount  INTEGER NOT NULL DEFAULT 0,
    stored_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS backups (
    id           INTEGER PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    sha256       TEXT    NOT NULL REFERENCES objects(sha256),
    size         INTEGER NOT NULL,
    backed_up_ts INTEGER NOT NULL,
    UNIQUE(file_id, sha256)
);

CREATE INDEX IF NOT EXISTS idx_backups_file ON backups(file_id);
CREATE INDEX IF NOT EXISTS idx_backups_sha  ON backups(sha256);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(name, path, body);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the index database with the schema applied."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)

    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row["value"]) != SCHEMA_VERSION:
        raise RuntimeError(
            f"index at {db_path} is schema v{row['value']}, this build expects v{SCHEMA_VERSION}"
        )
    return conn


def fts_quote(query: str) -> str:
    """Make an arbitrary user string safe for an FTS5 MATCH.

    Bare user input reaches MATCH as query *syntax*, so `find c++` or an
    unbalanced quote raises OperationalError. Each term is wrapped as a quoted
    phrase, which is almost always what someone typing into a search box meant.
    """
    terms = [t for t in query.replace('"', " ").split() if t]
    if not terms:
        return '""'
    return " ".join(f'"{t}"' for t in terms)
