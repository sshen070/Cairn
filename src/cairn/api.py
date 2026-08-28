"""The facade the CLI and the tests both drive.

Scanning deliberately does **not** hash file contents. Hashing means reading
every byte on the drive, and a scan is supposed to be the cheap operation you
can run over a whole disk. Change detection uses size plus mtime, and the
SHA-256 gets computed once, during backup, in the same pass that copies the
bytes into the store. That keeps `cairn scan` proportional to the number of
files rather than their total size.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import classify, db, extract, paths, walker
from .store import Store, hash_file


@dataclass
class ScanResult:
    roots: list[str]
    seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    dehydrated: int = 0
    errors: int = 0
    elapsed: float = 0.0
    skips: walker.SkipSummary = field(default_factory=walker.SkipSummary)


@dataclass
class BackupResult:
    considered: int = 0
    stored: int = 0
    deduplicated: int = 0
    already_backed_up: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    logical_bytes: int = 0
    physical_bytes: int = 0
    dry_run: bool = False


@dataclass
class VerifyResult:
    checked: int = 0
    corrupt: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class Hit:
    file_id: int
    path: str
    name: str
    category: str
    score: float
    size: int
    mtime_ns: int
    state: str
    snippet: str = ""


class Cairn:
    def __init__(self, db_path: str | Path, store_path: str | Path):
        self.db_path = Path(db_path)
        self.store = Store(store_path)
        self.conn: sqlite3.Connection = db.connect(self.db_path)
        self.store.initialize()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Cairn":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- scan ---------------------------------------------------------------

    def scan(
        self,
        roots: list[Path] | list[str],
        *,
        hydrate: bool = False,
        max_size: int | None = None,
    ) -> ScanResult:
        started = time.time()
        records, skips = walker.walk(roots, hydrate=hydrate, max_size=max_size)
        result = ScanResult(roots=[paths.normalize(r) for r in roots], skips=skips)
        now = int(time.time())

        with self.conn:
            for rec in records:
                result.seen += 1
                if rec.state == walker.DEHYDRATED:
                    result.dehydrated += 1
                elif rec.state == walker.ERROR:
                    result.errors += 1

                existing = self.conn.execute(
                    "SELECT id, size, mtime_ns, state FROM files WHERE path_key = ?",
                    (paths.path_key(rec.path),),
                ).fetchone()

                if (
                    existing is not None
                    and existing["size"] == rec.size
                    and existing["mtime_ns"] == rec.mtime_ns
                    and existing["state"] == rec.state
                ):
                    self.conn.execute(
                        "UPDATE files SET last_scan_ts = ?, atime_ns = ? WHERE id = ?",
                        (now, rec.atime_ns, existing["id"]),
                    )
                    result.unchanged += 1
                    continue

                self._index(rec, now)
                if existing is None:
                    result.added += 1
                else:
                    result.updated += 1

            self._remember_scan(result)

        result.elapsed = time.time() - started
        return result

    def _index(self, rec: walker.FileRecord, now: int) -> None:
        name = Path(rec.path).name
        body = extract.extract_text(rec.path, rec.ext) if rec.readable else ""
        searchable_name = extract.name_tokens(rec.path)
        searchable_path = extract.path_tokens(rec.path)

        result = classify.categorize(
            searchable_name,
            rec.path,
            body,
            ext=rec.ext,
        )

        cur = self.conn.execute(
            """
            INSERT INTO files (path, path_key, name, size, mtime_ns, atime_ns, ext,
                               category, confidence, score, state, skip_reason,
                               matched_rules, first_seen_ts, last_scan_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path_key) DO UPDATE SET
                path = excluded.path, name = excluded.name, size = excluded.size,
                mtime_ns = excluded.mtime_ns, atime_ns = excluded.atime_ns,
                ext = excluded.ext, category = excluded.category,
                confidence = excluded.confidence, score = excluded.score,
                state = excluded.state, skip_reason = excluded.skip_reason,
                matched_rules = excluded.matched_rules,
                last_scan_ts = excluded.last_scan_ts
            RETURNING id
            """,
            (
                rec.path,
                paths.path_key(rec.path),
                name,
                rec.size,
                rec.mtime_ns,
                rec.atime_ns,
                rec.ext,
                result.category,
                result.confidence,
                result.score,
                rec.state,
                rec.skip_reason,
                json.dumps(result.matched[:12]),
                now,
                now,
            ),
        )
        file_id = cur.fetchone()[0]

        # Explicit FTS sync, keyed to files.id. Delete-then-insert rather than
        # triggers, so the index can never drift from the table unnoticed.
        self.conn.execute("DELETE FROM search WHERE rowid = ?", (file_id,))
        self.conn.execute(
            "INSERT INTO search(rowid, name, path, body) VALUES (?,?,?,?)",
            (file_id, searchable_name, searchable_path, body),
        )

    def _remember_scan(self, result: ScanResult) -> None:
        payload = {
            "roots": result.roots,
            "seen": result.seen,
            "dehydrated": result.dehydrated,
            "skips": dict(result.skips.reasons),
            "pruned": dict(result.skips.dirs_pruned),
            "ts": int(time.time()),
        }
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES ('last_scan', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(payload),),
        )

    # -- search -------------------------------------------------------------

    def find(self, query: str, *, limit: int = 20, category: str | None = None) -> list[Hit]:
        sql = """
            SELECT f.id, f.path, f.name, f.category, f.score, f.size, f.mtime_ns, f.state,
                   snippet(search, 2, '[', ']', '...', 12) AS snip
            FROM search
            JOIN files f ON f.id = search.rowid
            WHERE search MATCH ?
        """
        params: list[object] = [db.fts_quote(query)]
        if category:
            sql += " AND f.category = ?"
            params.append(category)
        # Relevance first, then how much the file is worth keeping.
        sql += " ORDER BY bm25(search, 4.0, 2.0, 1.0), f.score DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(sql, params).fetchall()
        return [
            Hit(
                file_id=r["id"], path=r["path"], name=r["name"], category=r["category"],
                score=r["score"], size=r["size"], mtime_ns=r["mtime_ns"], state=r["state"],
                snippet=(r["snip"] or "").replace("\n", " "),
            )
            for r in rows
        ]

    # -- backup -------------------------------------------------------------

    def backup(
        self,
        *,
        all: bool = False,
        query: str | None = None,
        category: str | None = None,
        min_score: float | None = None,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> BackupResult:
        rows = self._selection(all=all, query=query, category=category, min_score=min_score, limit=limit)
        result = BackupResult(considered=len(rows), dry_run=dry_run)
        now = int(time.time())

        for row in rows:
            if dry_run:
                result.logical_bytes += row["size"]
                continue
            try:
                put = self.store.put(row["path"])
            except OSError as exc:
                result.failed.append((row["path"], f"{exc.__class__.__name__}: {exc}"))
                continue

            result.logical_bytes += put.size
            if put.deduplicated:
                result.deduplicated += 1
            else:
                result.physical_bytes += put.size

            with self.conn:
                self.conn.execute(
                    "INSERT INTO objects(sha256, size, refcount, stored_ts) VALUES (?,?,0,?) "
                    "ON CONFLICT(sha256) DO NOTHING",
                    (put.sha256, put.size, now),
                )
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO backups(file_id, sha256, size, backed_up_ts) VALUES (?,?,?,?)",
                    (row["id"], put.sha256, put.size, now),
                )
                if cur.rowcount:
                    self.conn.execute(
                        "UPDATE objects SET refcount = refcount + 1 WHERE sha256 = ?", (put.sha256,)
                    )
                    result.stored += 1
                else:
                    result.already_backed_up += 1
                self.conn.execute("UPDATE files SET sha256 = ? WHERE id = ?", (put.sha256, row["id"]))

        return result

    def _selection(
        self,
        *,
        all: bool,
        query: str | None,
        category: str | None,
        min_score: float | None,
        limit: int | None,
    ) -> list[sqlite3.Row]:
        # Only files whose bytes are actually readable can be backed up.
        # Placeholders stay catalogued but unbacked -- that is the point.
        if query:
            hits = self.find(query, limit=limit or 1000, category=category)
            if not hits:
                return []
            ids = [h.file_id for h in hits]
            marks = ",".join("?" * len(ids))
            sql = f"SELECT id, path, size FROM files WHERE id IN ({marks}) AND state = ?"
            params: list[object] = [*ids, walker.INDEXED]
        else:
            sql = "SELECT id, path, size FROM files WHERE state = ?"
            params = [walker.INDEXED]
            if not all and category is None and min_score is None:
                raise ValueError("backup needs a selector: all, query, category, or min_score")
            if category:
                sql += " AND category = ?"
                params.append(category)
            if min_score is not None:
                sql += " AND score >= ?"
                params.append(min_score)
            sql += " ORDER BY score DESC"
            if limit:
                sql += " LIMIT ?"
                params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    # -- restore ------------------------------------------------------------

    def recorded_sha(self, path: str | Path) -> str | None:
        """The digest recorded when this path was backed up, if it ever was."""
        row = self.conn.execute(
            """
            SELECT b.sha256 FROM backups b
            JOIN files f ON f.id = b.file_id
            WHERE f.path_key = ?
            ORDER BY b.backed_up_ts DESC LIMIT 1
            """,
            (paths.path_key(path),),
        ).fetchone()
        return row["sha256"] if row else None

    def restore(self, *, sha: str | None = None, path: str | Path | None = None, to: str | Path) -> Path:
        """Write a backed-up file to `to`, returning the path written."""
        if (sha is None) == (path is None):
            raise ValueError("restore takes exactly one of sha= or path=")

        if path is not None:
            digest = self.recorded_sha(path)
            if digest is None:
                raise KeyError(f"no backup recorded for {path}")
            filename = Path(paths.normalize(path)).name
        else:
            digest = sha
            assert digest is not None
            if not self.store.exists(digest):
                raise KeyError(f"object not in store: {digest}")
            row = self.conn.execute(
                "SELECT name FROM files WHERE sha256 = ? ORDER BY id LIMIT 1", (digest,)
            ).fetchone()
            filename = row["name"] if row else digest

        dest = Path(to) / filename
        return self.store.extract_to(digest, dest)

    def verify(self, *, sha: str | None = None) -> VerifyResult:
        """Rehash stored objects and confirm each still matches its digest."""
        if sha:
            digests = [sha]
        else:
            digests = [r["sha256"] for r in self.conn.execute("SELECT sha256 FROM objects ORDER BY sha256")]

        result = VerifyResult()
        for digest in digests:
            result.checked += 1
            if not self.store.exists(digest):
                result.missing.append(digest)
            elif hash_file(self.store.get(digest)) != digest:
                result.corrupt.append(digest)
        return result

    # -- status -------------------------------------------------------------

    def status(self) -> dict[str, object]:
        by_state = {
            r["state"]: r["n"]
            for r in self.conn.execute("SELECT state, COUNT(*) n FROM files GROUP BY state")
        }
        by_category = {
            r["category"]: r["n"]
            for r in self.conn.execute(
                "SELECT category, COUNT(*) n FROM files WHERE state = ? GROUP BY category ORDER BY n DESC",
                (walker.INDEXED,),
            )
        }
        logical = self.conn.execute("SELECT COALESCE(SUM(size),0) s FROM backups").fetchone()["s"]
        physical = self.conn.execute("SELECT COALESCE(SUM(size),0) s FROM objects").fetchone()["s"]
        n_objects = self.conn.execute("SELECT COUNT(*) n FROM objects").fetchone()["n"]
        n_backups = self.conn.execute("SELECT COUNT(*) n FROM backups").fetchone()["n"]

        deferred = self.conn.execute(
            "SELECT COUNT(*) n FROM files WHERE state = ? AND ext IN (%s)"
            % ",".join("?" * len(extract.DEFERRED_EXTENSIONS)),
            (walker.INDEXED, *sorted(extract.DEFERRED_EXTENSIONS)),
        ).fetchone()["n"]

        row = self.conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
        last_scan = json.loads(row["value"]) if row else None

        return {
            "by_state": by_state,
            "by_category": by_category,
            "objects": n_objects,
            "backups": n_backups,
            "logical_bytes": logical,
            "physical_bytes": physical,
            "dedup_ratio": (logical / physical) if physical else 1.0,
            "bytes_saved": max(0, logical - physical),
            "awaiting_extraction": deferred,
            "last_scan": last_scan,
        }
