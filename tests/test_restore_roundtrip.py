"""The gate.

Every file that gets backed up must come back byte-for-byte identical. This is
the only test that should ever block a commit. It was written before any of the
code it exercises, and it stays the first thing CI runs.
"""

from __future__ import annotations

import filecmp
import hashlib
from pathlib import Path

import pytest

from conftest import INDEXABLE_FILES


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@pytest.mark.parametrize("rel", INDEXABLE_FILES)
def test_restore_is_byte_identical(scanned, corpus: Path, tmp_path: Path, rel: str):
    original = corpus / rel
    dest = tmp_path / "restored"
    dest.mkdir(parents=True, exist_ok=True)

    restored = scanned.restore(path=original, to=dest)

    assert restored.exists(), f"restore produced no file for {rel}"
    assert restored.stat().st_size == original.stat().st_size, f"size differs for {rel}"
    assert sha256_of(restored) == sha256_of(original), f"content hash differs for {rel}"
    assert filecmp.cmp(original, restored, shallow=False), f"bytes differ for {rel}"


def test_recorded_hash_matches_the_original_bytes(scanned, corpus: Path):
    """The SHA stored at backup time must describe the file that was on disk."""
    mismatches = []
    for rel in INDEXABLE_FILES:
        original = corpus / rel
        recorded = scanned.recorded_sha(original)
        if recorded is None:
            mismatches.append(f"{rel}: no backup row")
        elif recorded != sha256_of(original):
            mismatches.append(f"{rel}: recorded {recorded[:12]} != actual")
    assert not mismatches, "recorded hashes disagree with disk:\n  " + "\n  ".join(mismatches)


def test_every_indexable_file_was_backed_up(scanned, corpus: Path):
    missing = [rel for rel in INDEXABLE_FILES if scanned.recorded_sha(corpus / rel) is None]
    assert not missing, f"{len(missing)} file(s) never made it into the store:\n  " + "\n  ".join(missing)


def test_verify_all_passes(scanned):
    """Rehashing every object on disk must agree with the index."""
    result = scanned.verify()
    assert result.corrupt == [], f"corrupt objects: {result.corrupt}"
    assert result.missing == [], f"missing objects: {result.missing}"
    assert result.checked > 0


def test_restore_by_hash(scanned, corpus: Path, tmp_path: Path):
    original = corpus / "tax" / "2023_Form_1040.pdf"
    sha = scanned.recorded_sha(original)
    assert sha is not None

    restored = scanned.restore(sha=sha, to=tmp_path)
    assert filecmp.cmp(original, restored, shallow=False)


def test_restore_refuses_unknown_target(scanned, tmp_path: Path):
    with pytest.raises(KeyError):
        scanned.restore(sha="0" * 64, to=tmp_path)
