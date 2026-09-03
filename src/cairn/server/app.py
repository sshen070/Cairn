"""The HTTP API.

Two properties in here are load-bearing and have tests to match:

**Uploads are re-hashed server-side.** The URL claims a digest; the server computes
the digest of the bytes that actually arrive and rejects a mismatch with 409. In a
content-addressed store this is not a nicety -- storing bytes under the wrong name
poisons the store permanently, because every future dedup hit on that digest hands
back the wrong content, and `verify` cannot tell because it compares against the
same wrong name. Never trust a client's arithmetic about content it supplies.

**Downloads are ACL-checked.** Knowing a digest is not authorization to fetch it.
An object is readable only if a device you can see holds a reference to it,
otherwise a device could enumerate hashes and read another device's documents.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..db import fts_quote
from ..store import Store
from .schema import connect, enrollment_secret, visible_device_ids

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
READ_BLOCK = 1 << 20

API = "/v1"


# ---------------------------------------------------------------- models


class EnrollRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    platform: str | None = Field(default=None, max_length=64)
    secret: str


class EnrollResponse(BaseModel):
    device_id: int
    name: str
    token: str


class HaveRequest(BaseModel):
    digests: list[str] = Field(default_factory=list, max_length=10_000)


class CatalogueRow(BaseModel):
    path: str
    path_key: str
    name: str
    size: int
    mtime_ns: int
    ext: str | None = None
    sha256: str | None = None
    category: str | None = None
    confidence: float | None = None
    score: float | None = None
    state: str
    body: str = ""


class CatalogueRequest(BaseModel):
    files: list[CatalogueRow] = Field(default_factory=list, max_length=5_000)


# ---------------------------------------------------------------- app


def create_app(db_path: str | Path, store_path: str | Path) -> FastAPI:
    conn = connect(db_path)
    store = Store(store_path)
    store.initialize()

    app = FastAPI(title="Cairn server", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.conn = conn
    app.state.store = store
    app.state.enroll_secret = enrollment_secret(conn)

    def current_device(authorization: str | None = Header(default=None)) -> sqlite3.Row:
        """Resolve the calling device from its bearer token.

        In this deployment the token identifies the device; the SSH tunnel (or,
        later, mTLS) authenticates the channel. When mTLS lands, the client
        certificate subject replaces this lookup and nothing else changes.
        """
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization[7:].strip()
        row = conn.execute("SELECT * FROM devices WHERE token = ?", (token,)).fetchone()
        if row is None:
            raise HTTPException(401, "unknown device token")
        conn.execute("UPDATE devices SET last_seen_ts = ? WHERE id = ?", (int(time.time()), row["id"]))
        return row

    # ------------------------------------------------------------ health

    @app.get(f"{API}/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service": "cairn",
            "devices": conn.execute("SELECT COUNT(*) n FROM devices").fetchone()["n"],
            "objects": conn.execute("SELECT COUNT(*) n FROM objects").fetchone()["n"],
        }

    # ------------------------------------------------------------ enrollment

    @app.post(f"{API}/devices/enroll", response_model=EnrollResponse)
    def enroll(req: EnrollRequest) -> EnrollResponse:
        import secrets as _secrets

        if not _secrets.compare_digest(req.secret, app.state.enroll_secret):
            raise HTTPException(403, "bad enrollment secret")

        existing = conn.execute("SELECT * FROM devices WHERE name = ?", (req.name,)).fetchone()
        if existing:
            # Re-enrolling an existing name reissues its token rather than
            # silently handing back the old one.
            token = _secrets.token_urlsafe(32)
            conn.execute("UPDATE devices SET token = ?, platform = ? WHERE id = ?",
                         (token, req.platform, existing["id"]))
            return EnrollResponse(device_id=existing["id"], name=req.name, token=token)

        token = _secrets.token_urlsafe(32)
        cur = conn.execute(
            "INSERT INTO devices(name, token, platform, enrolled_ts) VALUES (?,?,?,?) RETURNING id",
            (req.name, token, req.platform, int(time.time())),
        )
        return EnrollResponse(device_id=cur.fetchone()[0], name=req.name, token=token)

    @app.get(f"{API}/devices")
    def list_devices(device: sqlite3.Row = Depends(current_device)) -> dict:
        ids = visible_device_ids(conn, device["id"])
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, name, platform, enrolled_ts, last_seen_ts FROM devices WHERE id IN ({marks}) ORDER BY name",
            ids,
        ).fetchall()
        return {"you": device["name"], "visible": [dict(r) for r in rows]}

    # ------------------------------------------------------------ objects

    @app.post(f"{API}/objects/have")
    def objects_have(req: HaveRequest, device: sqlite3.Row = Depends(current_device)) -> dict:
        """Which of these digests the server already holds.

        One round trip for the whole batch. This is what makes an interrupted
        upload resumable: ask, then send only what is missing.
        """
        if not req.digests:
            return {"have": []}
        have = [d for d in req.digests if store.exists(d)]
        return {"have": have}

    @app.post(f"{API}/objects/claim")
    def objects_claim(req: HaveRequest, device: sqlite3.Row = Depends(current_device)) -> dict:
        """Register this device as a holder of objects the server already has.

        Deduplication and ownership are different things. When another device has
        already uploaded identical bytes there is nothing to transfer -- but the
        claiming device still needs a reference, or its push reports success while
        leaving it unable to download its own file. Uploading is one way to acquire
        a reference; this is the other.
        """
        claimed = 0
        for digest in req.digests:
            if store.exists(digest):
                _add_ref(conn, device["id"], digest)
                claimed += 1
        return {"claimed": claimed}

    @app.post(f"{API}/objects/verify")
    def objects_verify(req: HaveRequest, device: sqlite3.Row = Depends(current_device)) -> dict:
        """Re-hash stored objects and report which no longer match their name.

        Only the server can do this -- it is the only party holding the bytes.
        Scoped to what the caller may read, so this cannot be used to probe the
        integrity, or existence, of another device's objects.

        With no digests given, checks everything the caller has a reference to.
        That rehashes real data and is deliberately not the default anywhere.
        """
        if req.digests:
            # ACL only -- an object the caller owns but that has vanished from
            # disk must be reported as missing, not quietly dropped.
            wanted = [d for d in req.digests if _authorized(conn, device, d)]
        else:
            ids = visible_device_ids(conn, device["id"])
            marks = ",".join("?" * len(ids))
            wanted = [
                r["sha256"]
                for r in conn.execute(
                    f"SELECT DISTINCT sha256 FROM object_refs WHERE device_id IN ({marks})", ids
                )
            ]

        ok, corrupt, missing = [], [], []
        for digest in wanted:
            path = store.path_for(digest)
            if not path.exists():
                missing.append(digest)
                continue
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                while block := fh.read(READ_BLOCK):
                    h.update(block)
            (ok if h.hexdigest() == digest else corrupt).append(digest)

        return {"checked": len(wanted), "ok": len(ok), "corrupt": corrupt, "missing": missing}

    @app.head(f"{API}/objects/{{digest}}")
    def object_head(digest: str, device: sqlite3.Row = Depends(current_device)):
        if not _readable(conn, store, device, digest):
            raise HTTPException(404, "not found")
        return JSONResponse(content=None, status_code=200)

    @app.put(f"{API}/objects/{{digest}}")
    async def put_object(digest: str, request: Request, device: sqlite3.Row = Depends(current_device)) -> dict:
        digest = digest.lower()
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise HTTPException(400, "digest must be 64 hex characters")

        # Already present: record the reference and skip the transfer entirely.
        if store.exists(digest):
            _add_ref(conn, device["id"], digest)
            return {"stored": False, "deduplicated": True, "sha256": digest}

        store.initialize()
        h = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(dir=store.tmp, prefix="upload-")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "object exceeds maximum upload size")
                    h.update(chunk)
                    out.write(chunk)

            actual = h.hexdigest()
            if actual != digest:
                # The load-bearing check. Storing these bytes under the claimed
                # name would corrupt every future read of that digest.
                raise HTTPException(
                    409, f"digest mismatch: body hashes to {actual}, URL claimed {digest}"
                )

            dest = store.path_for(digest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, dest)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        now = int(time.time())
        conn.execute(
            "INSERT INTO objects(sha256, size, refcount, stored_ts) VALUES (?,?,0,?) "
            "ON CONFLICT(sha256) DO NOTHING",
            (digest, size, now),
        )
        _add_ref(conn, device["id"], digest)
        return {"stored": True, "deduplicated": False, "sha256": digest, "size": size}

    @app.get(f"{API}/objects/{{digest}}")
    def get_object(digest: str, device: sqlite3.Row = Depends(current_device)):
        if not _readable(conn, store, device, digest):
            # 404 rather than 403: not being allowed to read it and it not
            # existing should be indistinguishable, or the response leaks
            # which digests other devices hold.
            raise HTTPException(404, "not found")
        return FileResponse(store.path_for(digest), media_type="application/octet-stream")

    # ------------------------------------------------------------ catalogue

    @app.post(f"{API}/catalogue")
    def push_catalogue(req: CatalogueRequest, device: sqlite3.Row = Depends(current_device)) -> dict:
        now = int(time.time())
        upserted = 0
        for f in req.files:
            cur = conn.execute(
                """
                INSERT INTO catalogue (device_id, path, path_key, name, size, mtime_ns, ext,
                                       sha256, category, confidence, score, state, updated_ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(device_id, path_key) DO UPDATE SET
                    path=excluded.path, name=excluded.name, size=excluded.size,
                    mtime_ns=excluded.mtime_ns, ext=excluded.ext, sha256=excluded.sha256,
                    category=excluded.category, confidence=excluded.confidence,
                    score=excluded.score, state=excluded.state, updated_ts=excluded.updated_ts
                RETURNING id
                """,
                (device["id"], f.path, f.path_key, f.name, f.size, f.mtime_ns, f.ext,
                 f.sha256, f.category, f.confidence, f.score, f.state, now),
            )
            row_id = cur.fetchone()[0]
            conn.execute("DELETE FROM catalogue_search WHERE rowid = ?", (row_id,))
            conn.execute(
                "INSERT INTO catalogue_search(rowid, name, path, body) VALUES (?,?,?,?)",
                (row_id, f.name, f.path, f.body),
            )
            upserted += 1
        return {"upserted": upserted}

    @app.get(f"{API}/search")
    def search(q: str, limit: int = 20, device: sqlite3.Row = Depends(current_device)) -> dict:
        ids = visible_device_ids(conn, device["id"])
        marks = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""
            SELECT c.id, c.path, c.name, c.category, c.score, c.size, c.mtime_ns,
                   c.state, c.sha256, d.name AS device
            FROM catalogue_search s
            JOIN catalogue c ON c.id = s.rowid
            JOIN devices d   ON d.id = c.device_id
            WHERE catalogue_search MATCH ? AND c.device_id IN ({marks})
            ORDER BY bm25(catalogue_search, 4.0, 2.0, 1.0), c.score DESC
            LIMIT ?
            """,
            [fts_quote(q), *ids, limit],
        ).fetchall()
        return {"hits": [dict(r) for r in rows]}

    return app


# ---------------------------------------------------------------- helpers


def _add_ref(conn: sqlite3.Connection, device_id: int, digest: str) -> None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO object_refs(device_id, sha256, added_ts) VALUES (?,?,?)",
        (device_id, digest, int(time.time())),
    )
    if cur.rowcount:
        conn.execute("UPDATE objects SET refcount = refcount + 1 WHERE sha256 = ?", (digest,))


def _authorized(conn: sqlite3.Connection, device: sqlite3.Row, digest: str) -> bool:
    """Whether this device is entitled to know anything about `digest`.

    Purely an ACL question -- deliberately separate from whether the object is
    actually on disk. Conflating the two makes a *missing* object indistinguish-
    able from a forbidden one, which is right for a download (both are 404) and
    wrong for verification, where a vanished object is the finding.
    """
    if len(digest) != 64:
        return False
    ids = visible_device_ids(conn, device["id"])
    marks = ",".join("?" * len(ids))
    row = conn.execute(
        f"SELECT 1 FROM object_refs WHERE sha256 = ? AND device_id IN ({marks}) LIMIT 1",
        [digest, *ids],
    ).fetchone()
    return row is not None


def _readable(conn: sqlite3.Connection, store: Store, device: sqlite3.Row, digest: str) -> bool:
    """Readable = allowed to see it *and* the bytes are there."""
    return _authorized(conn, device, digest) and store.exists(digest)
