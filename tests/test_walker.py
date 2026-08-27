"""Walker behaviour: what gets skipped, what survives, and what never opens."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from cairn import extract, paths, walker

from conftest import SKIPPED_DIRS


def paths_of(records) -> set[str]:
    return {r.path for r in records}


# -- skip lists -------------------------------------------------------------


def test_skip_lists_prune_known_directories(corpus: Path):
    records, summary = walker.walk([corpus])
    found = paths_of(records)

    for skipped in SKIPPED_DIRS:
        offenders = [p for p in found if f"{os.sep}{skipped}{os.sep}" in p]
        assert not offenders, f"{skipped} should have been pruned, got {offenders[:3]}"

    assert summary.dirs_pruned, "pruning happened but was not accounted for"


def test_skip_summary_is_never_silent(corpus: Path):
    _, summary = walker.walk([corpus])
    rendered = summary.render()
    assert rendered.strip()
    # Every pruned directory must be nameable, not folded into an opaque total.
    assert any(name in rendered for name in ("node_modules", ".git", "__pycache__"))


def test_explicit_root_overrides_the_skip_list(tmp_path: Path):
    """A directory named on the command line is scanned even if the skip list
    would otherwise prune it. The lists exist to stop a broad walk wandering
    into system locations, not to veto a direct request.

    Regression test: skip fragments were once matched against the absolute
    path, so any corpus living under AppData\\Local\\Temp vanished entirely.
    """
    root = tmp_path / "node_modules"
    root.mkdir()
    (root / "important.txt").write_text("lease agreement", encoding="utf-8")

    records, _ = walker.walk([root])
    assert len(records) == 1, "explicitly requested root was pruned by the skip list"


def test_skip_fragments_still_apply_below_the_root(tmp_path: Path):
    root = tmp_path / "data"
    buried = root / "appdata" / "local" / "temp"
    buried.mkdir(parents=True)
    (buried / "junk.txt").write_text("x", encoding="utf-8")
    (root / "keep.txt").write_text("y", encoding="utf-8")

    records, summary = walker.walk([root])
    names = {Path(r.path).name for r in records}
    assert names == {"keep.txt"}
    assert summary.dirs_pruned["system location"] == 1


# -- awkward paths ----------------------------------------------------------


def test_unicode_filenames_survive(corpus: Path):
    records, _ = walker.walk([corpus])
    names = {Path(r.path).name for r in records}
    assert "Ünïcödé_résumé.pdf" in names
    assert "履歴書_2024.pdf" in names


def test_long_paths_are_walked_and_normalized(corpus: Path):
    records, _ = walker.walk([corpus])
    long_ones = [r for r in records if len(r.path) > 260]
    assert long_ones, "the >260 character fixture was not reached"
    for rec in long_ones:
        # Stored form must be the plain path, never the extended-length form.
        assert not rec.path.startswith(paths._EXTENDED_PREFIX)
        assert os.path.exists(paths.long_path(rec.path))


def test_zero_byte_and_extensionless_files_are_indexed(corpus: Path):
    records, _ = walker.walk([corpus])
    by_name = {Path(r.path).name: r for r in records}

    assert by_name["empty.txt"].size == 0
    assert by_name["empty.txt"].state == walker.INDEXED
    assert by_name["LICENSE"].ext == ""


def test_paths_are_absolute_and_nfc(corpus: Path):
    records, _ = walker.walk([corpus])
    import unicodedata

    for rec in records:
        assert os.path.isabs(rec.path)
        assert rec.path == unicodedata.normalize("NFC", rec.path)


# -- cloud placeholders -----------------------------------------------------


class FakeStat:
    """Minimal stand-in for os.stat_result carrying Windows attributes."""

    def __init__(self, attrs: int):
        self.st_file_attributes = attrs


@pytest.mark.parametrize(
    "attr",
    [
        walker.FILE_ATTRIBUTE_OFFLINE,
        walker.FILE_ATTRIBUTE_RECALL_ON_OPEN,
        walker.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    ],
)
def test_each_placeholder_attribute_is_recognized(attr: int):
    assert walker.is_dehydrated(FakeStat(attr))


def test_ordinary_files_are_not_mistaken_for_placeholders():
    assert not walker.is_dehydrated(FakeStat(0))
    assert not walker.is_dehydrated(FakeStat(0x20))  # FILE_ATTRIBUTE_ARCHIVE


def test_placeholders_are_catalogued_but_never_opened(corpus: Path, tmp_path: Path, monkeypatch):
    """The load-bearing safety property.

    With every file reported as a cloud stub, a scan must still produce rows,
    must mark them `dehydrated`, and must not read a single byte -- extraction
    is rigged to explode if it is ever called.
    """
    monkeypatch.setattr(walker, "is_dehydrated", lambda st: True)

    def explode(*a, **k):
        raise AssertionError("a cloud placeholder was opened; this would trigger a download")

    monkeypatch.setattr(extract, "extract_text", explode)

    from cairn.api import Cairn

    c = Cairn(db_path=tmp_path / "c.db", store_path=tmp_path / "s")
    result = c.scan([corpus])

    assert result.seen > 0
    assert result.dehydrated == result.seen
    states = {r["state"] for r in c.conn.execute("SELECT DISTINCT state FROM files")}
    assert states == {walker.DEHYDRATED}

    # And nothing dehydrated is eligible for backup.
    assert c.backup(all=True).considered == 0
    c.close()


def test_hydrate_flag_opts_back_in(corpus: Path, monkeypatch):
    monkeypatch.setattr(walker, "is_dehydrated", lambda st: True)
    records, _ = walker.walk([corpus], hydrate=True)
    assert records and all(r.state == walker.INDEXED for r in records)


# -- loops and links --------------------------------------------------------


def test_symlink_loop_terminates(tmp_path: Path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "file.txt").write_text("hello", encoding="utf-8")
    try:
        os.symlink(root, root / "sub" / "loop", target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlink creation not permitted here")

    records, summary = walker.walk([root])  # must return rather than recurse forever
    assert any(Path(r.path).name == "file.txt" for r in records)
    assert summary.reasons["symlink not followed"] >= 1


def test_missing_root_is_an_explicit_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        walker.walk([tmp_path / "does-not-exist"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")
def test_path_key_folds_case_on_windows(tmp_path: Path):
    p = tmp_path / "Mixed_Case.txt"
    p.write_text("x", encoding="utf-8")
    assert paths.path_key(str(p).upper()) == paths.path_key(str(p).lower())


def test_max_size_marks_rather_than_drops(corpus: Path):
    records, summary = walker.walk([corpus], max_size=10)
    oversized = [r for r in records if r.state == walker.SKIPPED]
    assert oversized, "max-size had no effect"
    # Catalogued, not forgotten: knowing the file exists is the whole premise.
    assert all(r.size > 10 for r in oversized)
    assert summary.reasons["larger than max-size"] == len(oversized)
