"""Indexing and search: the `cairn find tax` gate, and re-scan behaviour."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cairn import db


def names(hits) -> list[str]:
    return [Path(h.path).name for h in hits]


# -- the gate ---------------------------------------------------------------


def test_find_tax_returns_the_1040(scanned):
    """Phase 1's stated gate: type a word, get the real document back."""
    hits = scanned.find("tax")
    assert hits, "no results for 'tax'"
    assert "2023_Form_1040.pdf" in names(hits)


def test_find_matches_pdf_by_filename_alone(scanned):
    """PDF text extraction is Phase 2, so a PDF must still be reachable by name."""
    hits = scanned.find("1040")
    assert "2023_Form_1040.pdf" in names(hits)


def test_find_matches_body_text_not_just_names(scanned):
    """'landlord' appears only inside the lease body, never in a filename."""
    hits = scanned.find("landlord")
    assert hits, "body text is not being indexed"
    assert all("landlord" not in n.lower() for n in names(hits))
    assert "lease_terms_summary.txt" in names(hits)


def test_docx_body_is_extracted_without_a_pdf_library(scanned):
    """DOCX is a zip of XML, so its text is free. 'whereof' is body-only."""
    hits = scanned.find("whereof")
    assert "lease_agreement_signed.docx" in names(hits)


def test_underscored_filenames_are_tokenized(scanned):
    """`W2_Acme_2023.pdf` should be findable by 'acme', not only the full stem."""
    assert "W2_Acme_2023.pdf" in names(scanned.find("acme"))


def test_search_is_case_insensitive(scanned):
    assert names(scanned.find("PASSPORT")) == names(scanned.find("passport"))


def test_category_filter_narrows_results(scanned):
    hits = scanned.find("statement", category="financial")
    assert hits
    assert {h.category for h in hits} == {"financial"}


def test_no_match_returns_empty(scanned):
    assert scanned.find("zzzznonexistentterm") == []


# -- query sanitization -----------------------------------------------------


@pytest.mark.parametrize("query", ['c++', 'AND', 'foo"bar', '"', 'a OR', 'NEAR(', '*', '-', ''])
def test_hostile_queries_do_not_raise(scanned, query):
    """Raw user text reaches FTS5 as query *syntax*; it has to be quoted first."""
    scanned.find(query)


def test_fts_quote_wraps_each_term():
    assert db.fts_quote("tax return") == '"tax" "return"'
    assert db.fts_quote('  ') == '""'


# -- re-scan ----------------------------------------------------------------


def test_rescan_is_idempotent(cairn, corpus: Path):
    first = cairn.scan([corpus])
    rows_after_first = cairn.conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]

    second = cairn.scan([corpus])
    rows_after_second = cairn.conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]

    assert rows_after_second == rows_after_first, "re-scan duplicated rows"
    assert second.added == 0
    assert second.unchanged == first.seen


def test_rescan_leaves_no_orphaned_search_rows(cairn, corpus: Path):
    cairn.scan([corpus])
    cairn.scan([corpus])
    files = cairn.conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
    search = cairn.conn.execute("SELECT COUNT(*) n FROM search").fetchone()["n"]
    assert search == files, "FTS index drifted from the files table"


def test_modified_file_is_reindexed(cairn, tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    doc = root / "note.txt"
    doc.write_text("nothing interesting here", encoding="utf-8")
    cairn.scan([root])
    assert not cairn.find("landlord")

    # Rewrite with different content *and* a different size, which is what
    # change detection keys on.
    doc.write_text("landlord and tenant lease agreement, security deposit", encoding="utf-8")
    result = cairn.scan([root])

    assert result.updated == 1
    assert names(cairn.find("landlord")) == ["note.txt"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows is case-insensitive")
def test_case_variant_path_does_not_double_insert(cairn, tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "Note.txt").write_text("hello", encoding="utf-8")

    cairn.scan([root])
    cairn.scan([Path(str(root).upper())])

    assert cairn.conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"] == 1


# -- dedup ------------------------------------------------------------------


def test_identical_content_stores_one_object(scanned):
    """Content addressing means duplicate bytes cost storage once."""
    backups = scanned.conn.execute("SELECT COUNT(*) n FROM backups").fetchone()["n"]
    objects = scanned.conn.execute("SELECT COUNT(*) n FROM objects").fetchone()["n"]
    assert objects < backups, "the duplicate fixtures did not deduplicate"

    dupes = scanned.conn.execute(
        "SELECT sha256, COUNT(*) n FROM backups GROUP BY sha256 HAVING n > 1"
    ).fetchall()
    assert dupes, "expected at least one hash backing multiple files"


def test_refcount_matches_backup_rows(scanned):
    mismatched = scanned.conn.execute(
        """
        SELECT o.sha256, o.refcount, COUNT(b.id) actual
        FROM objects o LEFT JOIN backups b ON b.sha256 = o.sha256
        GROUP BY o.sha256 HAVING o.refcount != actual
        """
    ).fetchall()
    assert not mismatched, f"refcounts drifted: {[tuple(r) for r in mismatched]}"


def test_status_reports_a_dedup_ratio(scanned):
    s = scanned.status()
    assert s["dedup_ratio"] > 1.0
    assert s["bytes_saved"] > 0
    assert s["objects"] > 0


def test_backup_requires_a_selector(cairn, corpus: Path):
    cairn.scan([corpus])
    with pytest.raises(ValueError):
        cairn.backup()


def test_dry_run_writes_nothing(cairn, corpus: Path):
    cairn.scan([corpus])
    r = cairn.backup(all=True, dry_run=True)
    assert r.considered > 0
    assert cairn.store.object_count() == 0


def test_changed_file_clears_its_stored_digest(cairn, tmp_path):
    """A digest describes specific bytes; when they change it must not survive.

    Regression, found deploying to a real server: regenerated PDF/DOCX fixtures
    kept the digest of their previous contents, so `push` announced an old hash
    while uploading new bytes. Only the server's re-hash check caught it (409).
    Text fixtures hid the bug because they regenerate byte-identically.
    """
    root = tmp_path / "docs"
    root.mkdir()
    doc = root / "statement.txt"
    doc.write_text("original content", encoding="utf-8")

    cairn.scan([root])
    cairn.backup(all=True)
    stored = cairn.conn.execute("SELECT sha256 FROM files").fetchone()["sha256"]
    assert stored, "backup should record a digest"

    doc.write_text("completely different content now", encoding="utf-8")
    cairn.scan([root])

    after = cairn.conn.execute("SELECT sha256 FROM files").fetchone()["sha256"]
    assert after is None, "a stale digest survived a content change"


def test_unchanged_file_keeps_its_digest(cairn, tmp_path):
    """The counterpart: re-scanning must not throw away still-valid work."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "statement.txt").write_text("stable content", encoding="utf-8")

    cairn.scan([root])
    cairn.backup(all=True)
    before = cairn.conn.execute("SELECT sha256 FROM files").fetchone()["sha256"]

    cairn.scan([root])
    after = cairn.conn.execute("SELECT sha256 FROM files").fetchone()["sha256"]
    assert after == before


def test_v1_index_migrates_forward(tmp_path):
    """An existing index must upgrade in place, not refuse to open."""
    import sqlite3
    from cairn import db as dbmod
    from cairn.api import Cairn

    path = tmp_path / "old.db"
    c = Cairn(db_path=path, store_path=tmp_path / "store")
    c.conn.execute("INSERT INTO files (path, path_key, name, size, mtime_ns, state, "
                   "first_seen_ts, last_scan_ts) VALUES ('/x/a.txt','/x/a.txt','a.txt',1,1,'indexed',0,0)")
    c.conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    c.conn.execute("DROP TABLE remote_state")
    c.close()

    reopened = dbmod.connect(path)
    assert reopened.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    assert reopened.execute("SELECT 1 FROM sqlite_master WHERE name='remote_state'").fetchone()
    assert reopened.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1, "rows must survive"


def test_index_from_a_newer_build_is_refused(tmp_path):
    """Writing to a schema we do not understand is worse than failing."""
    from cairn import db as dbmod
    from cairn.api import Cairn

    path = tmp_path / "future.db"
    c = Cairn(db_path=path, store_path=tmp_path / "store")
    c.conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    c.close()

    with pytest.raises(RuntimeError, match="only understands"):
        dbmod.connect(path)
