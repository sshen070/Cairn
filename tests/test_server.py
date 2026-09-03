"""Phase 4: agent and server split across a real HTTP connection.

These tests run a real uvicorn server on a random loopback port and drive it with
the real stdlib agent client -- not FastAPI's TestClient. The point is to exercise
the wire: headers, streaming uploads, status codes, and the agent's own HTTP
handling, all of which TestClient would bypass.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

uvicorn = pytest.importorskip("uvicorn", reason="server extra not installed")

from cairn.api import Cairn  # noqa: E402
from cairn.client import CairnClient, RemoteError, enroll  # noqa: E402
from cairn.server.app import create_app  # noqa: E402
from cairn.server.schema import connect as server_connect, enrollment_secret  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunningServer:
    def __init__(self, root: Path):
        self.db = root / "server.db"
        self.store = root / "server-store"
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.secret = enrollment_secret(server_connect(self.db))

        config = uvicorn.Config(create_app(self.db, self.store), host="127.0.0.1",
                                port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> "RunningServer":
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture
def server(tmp_path_factory):
    """Function-scoped on purpose.

    A shared server makes these tests order-dependent: one test's upload is
    already on the server when the next asks whether anything transferred, so
    `uploaded` silently reads zero. A fresh server per test costs ~0.2s and
    removes a whole class of confusing failures.
    """
    s = RunningServer(tmp_path_factory.mktemp("server")).start()
    yield s
    s.stop()


@pytest.fixture
def device_a(server, tmp_path):
    return _agent(server, tmp_path, "laptop-a")


@pytest.fixture
def device_b(server, tmp_path):
    return _agent(server, tmp_path, "laptop-b")


def _agent(server, tmp_path: Path, name: str) -> Cairn:
    root = tmp_path / name
    c = Cairn(db_path=root / "cairn.db", store_path=root / "store")
    c.set_remote(enroll(server.url, name, server.secret, platform="test"))
    return c


# -- basics -----------------------------------------------------------------


def test_health_needs_no_token(server):
    client = CairnClient(type("C", (), {"url": server.url, "token": "",
                                        "base": server.url + "/v1"})())
    assert client.health()["status"] == "ok"


def test_enrollment_requires_the_secret(server):
    with pytest.raises(RemoteError) as exc:
        enroll(server.url, "impostor", "not-the-secret")
    assert exc.value.status == 403


def test_unknown_token_is_rejected(server):
    from cairn.client import RemoteConfig

    client = CairnClient(RemoteConfig(url=server.url, token="garbage", device_name="x"))
    with pytest.raises(RemoteError) as exc:
        client.devices()
    assert exc.value.status == 401


# -- the upload integrity property ------------------------------------------


def test_server_rejects_bytes_that_do_not_match_the_claimed_digest(server, device_a, tmp_path):
    """The load-bearing check.

    Storing bytes under the wrong digest poisons a content-addressed store
    permanently: every later dedup hit returns the wrong content, and verify
    cannot detect it because it compares against the same wrong name.
    """
    client = device_a.client()
    f = tmp_path / "payload.txt"
    f.write_text("the real content", encoding="utf-8")

    lie = "a" * 64
    with pytest.raises(RemoteError) as exc:
        client.put_object(lie, f)

    assert exc.value.status == 409
    assert "mismatch" in exc.value.message.lower()
    assert not client.have([lie]), "rejected bytes must not have been stored"


def test_malformed_digest_is_refused(server, device_a, tmp_path):
    client = device_a.client()
    f = tmp_path / "x.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(RemoteError) as exc:
        client.put_object("not-a-digest", f)
    assert exc.value.status == 400


# -- per-device isolation: the Phase 4 gate ---------------------------------


def test_devices_cannot_read_each_others_objects(server, device_a, device_b, corpus, tmp_path):
    """Knowing a digest is not authorization to fetch it.

    Without this, any enrolled device could enumerate hashes and read every other
    device's documents -- the exact failure the per-device scope exists to prevent.
    """
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)

    digest = device_a.conn.execute(
        "SELECT sha256 FROM files WHERE sha256 IS NOT NULL LIMIT 1"
    ).fetchone()["sha256"]

    # A can read its own object.
    assert device_a.client().have([digest]) == {digest}
    device_a.client().get_object(digest, tmp_path / "a-copy.bin")

    # B knows the digest but holds no reference to it.
    with pytest.raises(RemoteError) as exc:
        device_b.client().get_object(digest, tmp_path / "b-copy.bin")
    assert exc.value.status == 404, "must be indistinguishable from 'does not exist'"


def test_search_is_scoped_to_visible_devices(server, device_a, device_b, corpus):
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)
    device_b.scan([corpus / "medical"])
    device_b.push(all=True)

    a_hits = device_a.find_remote("tax")
    b_hits = device_b.find_remote("tax")

    assert a_hits, "a device must see its own documents"
    assert {h["device"] for h in a_hits} == {"laptop-a"}
    assert b_hits == [], "laptop-b was never granted sight of laptop-a"


def test_grant_makes_another_device_visible(server, device_a, device_b, corpus):
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)

    assert device_b.find_remote("tax") == []

    conn = server_connect(server.db)
    a_id = conn.execute("SELECT id FROM devices WHERE name='laptop-a'").fetchone()["id"]
    b_id = conn.execute("SELECT id FROM devices WHERE name='laptop-b'").fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO device_grants(viewer_id, visible_id, granted_ts) VALUES (?,?,?)",
        (b_id, a_id, int(time.time())),
    )

    hits = device_b.find_remote("tax")
    assert hits and {h["device"] for h in hits} == {"laptop-a"}


# -- push / pull roundtrip --------------------------------------------------


def test_push_then_pull_is_byte_identical(server, device_a, corpus, tmp_path):
    device_a.scan([corpus / "tax"])
    result = device_a.push(all=True)
    assert result.uploaded > 0
    assert not result.failed

    original = corpus / "tax" / "2023_Form_1040.pdf"
    out = device_a.pull(path=original, to=tmp_path / "pulled")

    import filecmp

    assert filecmp.cmp(original, out, shallow=False)


def test_second_push_transfers_nothing(server, device_a, corpus):
    """Resumability and dedup are the same mechanism: ask, then send only what is missing."""
    device_a.scan([corpus / "tax"])
    first = device_a.push(all=True)
    second = device_a.push(all=True)

    assert first.uploaded > 0
    assert second.uploaded == 0
    assert second.already_on_server == first.considered
    assert second.transfer_saved_ratio == 1.0


def test_pull_rejects_bytes_that_fail_verification(server, device_a, corpus, tmp_path, monkeypatch):
    """A server returning wrong bytes must not silently overwrite a good copy."""
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)
    original = corpus / "tax" / "2023_Form_1040.pdf"

    from cairn.client import CairnClient as RealClient

    def corrupted(self, digest, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not the bytes you asked for")
        return dest

    monkeypatch.setattr(RealClient, "get_object", corrupted)

    with pytest.raises(RuntimeError, match="discarded"):
        device_a.pull(path=original, to=tmp_path / "bad")
    assert not (tmp_path / "bad" / original.name).exists()


def test_dehydrated_files_are_catalogued_but_not_uploaded(server, device_a, corpus, monkeypatch):
    """The thesis, across the network: knowledge travels even when bytes cannot."""
    from cairn import walker

    monkeypatch.setattr(walker, "is_dehydrated", lambda st: True)
    device_a.scan([corpus / "tax"])

    result = device_a.push(all=True)
    assert result.considered == 0, "placeholders have no bytes to send"

    # But the rows still exist locally and can be catalogued upstream.
    n = device_a.conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
    assert n > 0


def test_deduplicated_push_still_grants_ownership(server, device_a, device_b, corpus, tmp_path):
    """Dedup must not cost the second device access to its own file.

    Regression: when device A had already uploaded identical bytes, device B's
    push skipped the transfer *and* never claimed a reference. The push reported
    success and B could not retrieve its own file -- silent false success, the
    worst failure mode a backup tool has.
    """
    device_a.scan([corpus / "medical"])
    device_a.push(all=True)

    device_b.scan([corpus / "medical"])
    result = device_b.push(all=True)

    assert result.uploaded == 0, "identical bytes should not transfer twice"
    assert result.already_on_server > 0

    original = corpus / "medical" / "visit_summary_feb2024.txt"
    out = device_b.pull(path=original, to=tmp_path / "b-pulled")

    import filecmp

    assert filecmp.cmp(original, out, shallow=False)


def test_push_after_content_change_does_not_send_a_stale_digest(server, device_a, tmp_path):
    """End-to-end version of the bug that showed up against the real Pi.

    Binary fixtures regenerate with new bytes; their rows kept the old digest,
    so push claimed a hash the body no longer had and the server returned 409
    for 11 of 28 files.
    """
    root = tmp_path / "docs"
    root.mkdir()
    doc = root / "report.txt"
    doc.write_text("first version", encoding="utf-8")

    device_a.scan([root])
    assert device_a.push(all=True).uploaded == 1

    doc.write_text("second version, different bytes entirely", encoding="utf-8")
    device_a.scan([root])
    result = device_a.push(all=True)

    assert not result.failed, f"push failed after a content change: {result.failed}"
    assert result.uploaded == 1

    out = device_a.pull(path=doc, to=tmp_path / "back")
    assert out.read_text(encoding="utf-8") == "second version, different bytes entirely"


def test_push_recovers_when_a_file_changes_mid_flight(server, device_a, tmp_path, monkeypatch):
    """The race the server's 409 exists to catch: edited between hash and send.

    Rehash once and retry, rather than failing the whole push over an edit that
    landed while it was running.
    """
    root = tmp_path / "docs"
    root.mkdir()
    doc = root / "live.txt"
    doc.write_text("before", encoding="utf-8")
    device_a.scan([root])

    from cairn.client import CairnClient

    real_put = CairnClient.put_object
    swapped = {"done": False}

    def put_then_change(self, digest, path):
        # Rewrite the file the first time, so the digest just computed is wrong.
        if not swapped["done"]:
            swapped["done"] = True
            Path(path).write_text("after -- changed mid-push", encoding="utf-8")
        return real_put(self, digest, path)

    monkeypatch.setattr(CairnClient, "put_object", put_then_change)

    result = device_a.push(all=True)
    assert not result.failed, f"push did not recover: {result.failed}"
    assert result.uploaded == 1


def test_pull_prefers_current_content_over_local_backup_history(server, device_a, tmp_path):
    """`backups` is a local archive log, not a statement about the server.

    Regression: after a file was backed up locally and then edited, `pull`
    resolved its digest from the stale `backups` row and asked the server for
    an object it had never been sent -- 404 for a file sitting right there under
    its current digest.
    """
    root = tmp_path / "docs"
    root.mkdir()
    doc = root / "policy.txt"
    doc.write_text("version one", encoding="utf-8")

    device_a.scan([root])
    device_a.backup(all=True)          # writes a local `backups` row
    doc.write_text("version two, quite different", encoding="utf-8")
    device_a.scan([root])
    device_a.push(all=True)            # only version two reaches the server

    out = device_a.pull(path=doc, to=tmp_path / "back")
    assert out.read_text(encoding="utf-8") == "version two, quite different"


# -- remote diff: what of mine is not backed up -----------------------------


def test_diff_reports_missing_then_covered(server, device_a, corpus):
    device_a.scan([corpus / "tax"])

    before = device_a.remote_diff()
    assert before.backed_up == 0
    assert before.not_backed_up, "nothing pushed yet, so everything should be listed"
    assert before.coverage == 0.0

    device_a.push(all=True)

    after = device_a.remote_diff()
    assert after.not_backed_up == []
    assert after.coverage == 1.0
    assert after.backed_up == before.total


def test_diff_works_with_the_server_unreachable(server, device_a, corpus):
    """The whole reason the cache exists: answerable with the tunnel closed."""
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)
    server.stop()

    result = device_a.remote_diff()          # no network call
    assert result.coverage == 1.0
    assert not result.refreshed


def test_editing_a_file_expires_its_cached_claim(server, device_a, tmp_path):
    """A cached row counts only while its digest still matches the file."""
    root = tmp_path / "docs"
    root.mkdir()
    doc = root / "note.txt"
    doc.write_text("first", encoding="utf-8")

    device_a.scan([root])
    device_a.push(all=True)
    assert device_a.remote_diff().coverage == 1.0

    doc.write_text("second, different content", encoding="utf-8")
    device_a.scan([root])

    result = device_a.remote_diff()
    assert result.coverage == 0.0, "an edited file is not backed up, cache notwithstanding"
    assert len(result.not_backed_up) == 1


def test_refresh_drops_claims_the_server_no_longer_backs(server, device_a, corpus):
    """The direction the cache cannot detect on its own: pruning on the server."""
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)
    assert device_a.remote_diff().coverage == 1.0

    for obj in (server.store / "objects").rglob("*"):
        if obj.is_file():
            obj.unlink()

    stale = device_a.remote_diff()
    assert stale.coverage == 1.0, "cache cannot know without asking"

    fresh = device_a.remote_diff(refresh=True)
    assert fresh.refreshed
    assert fresh.coverage == 0.0, "--refresh must notice the objects are gone"


def test_dehydrated_files_are_not_counted_against_coverage(server, device_a, corpus, monkeypatch):
    """Placeholders have no bytes to send, so they are not a backup shortfall."""
    from cairn import walker

    monkeypatch.setattr(walker, "is_dehydrated", lambda st: True)
    device_a.scan([corpus / "tax"])

    result = device_a.remote_diff()
    assert result.total == 0
    assert result.unbackable > 0
    assert result.coverage == 1.0


# -- remote verify: does the server still have good bytes -------------------


def test_remote_verify_reports_intact_objects(server, device_a, corpus):
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)

    result = device_a.remote_verify()
    assert result.checked > 0
    assert result.ok == result.checked
    assert result.corrupt == []
    assert result.missing == []


def test_remote_verify_detects_corruption_on_the_server(server, device_a, corpus):
    """Bit rot on the Pi's disk. Nothing else in the system can see this."""
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)

    victim = next(p for p in (server.store / "objects").rglob("*") if p.is_file())
    victim.write_bytes(b"corrupted on disk, digest no longer matches")

    result = device_a.remote_verify()
    assert victim.name in result.corrupt
    assert result.ok == result.checked - 1


def test_remote_verify_detects_a_missing_object(server, device_a, corpus):
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)

    victim = next(p for p in (server.store / "objects").rglob("*") if p.is_file())
    name = victim.name
    victim.unlink()

    result = device_a.remote_verify()
    assert name in result.missing


def test_remote_verify_cannot_reach_another_devices_objects(server, device_a, device_b, corpus):
    """Scoped like every other read: you cannot probe what you may not see."""
    device_a.scan([corpus / "tax"])
    device_a.push(all=True)
    digests = [r["sha256"] for r in device_a.conn.execute(
        "SELECT DISTINCT sha256 FROM remote_state")]
    assert digests

    body = device_b.client().verify_objects(digests)
    assert body["checked"] == 0, "device B must not be able to verify device A's objects"
