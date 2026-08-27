"""Shared test corpus.

The corpus is built once per session, at import time, so test modules can
parametrize over the real file list rather than looping inside a single test.
That way a single broken file names itself in the failure output.
"""

from __future__ import annotations

import atexit
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import make_fixtures  # noqa: E402

# Directory names the walker is expected to skip wholesale. Declared literally
# here rather than imported from cairn.paths so the tests stay an independent
# statement of intent -- if the production skip list changes, these fail loudly.
SKIPPED_DIRS = {"node_modules", ".git", "__pycache__", ".venv"}

CORPUS = Path(tempfile.mkdtemp(prefix="cairn-corpus-"))
MANIFEST: dict[str, str] = make_fixtures.build(CORPUS)
atexit.register(shutil.rmtree, CORPUS, ignore_errors=True)


def _relative_files(root: Path) -> list[str]:
    out = []
    for p in root.rglob("*"):
        if p.is_file():
            out.append(p.relative_to(root).as_posix())
    return sorted(out)


ALL_FILES: list[str] = _relative_files(CORPUS)
"""Every file in the corpus, including ones the walker should skip."""

INDEXABLE_FILES: list[str] = [
    rel for rel in ALL_FILES if not SKIPPED_DIRS.intersection(Path(rel).parts)
]
"""Files a scan is expected to actually index."""


@pytest.fixture(scope="session")
def corpus() -> Path:
    return CORPUS


@pytest.fixture(scope="session")
def manifest() -> dict[str, str]:
    return MANIFEST


@pytest.fixture
def cairn(tmp_path: Path):
    """A fresh, empty Cairn instance backed by its own db and store."""
    from cairn.api import Cairn

    return Cairn(db_path=tmp_path / "cairn.db", store_path=tmp_path / "store")


@pytest.fixture(scope="session")
def scanned(tmp_path_factory):
    """One shared instance with the corpus scanned and everything backed up.

    Session-scoped because scanning and hashing the corpus is the slow part;
    the tests that use it only read.
    """
    from cairn.api import Cairn

    d = tmp_path_factory.mktemp("scanned")
    c = Cairn(db_path=d / "cairn.db", store_path=d / "store")
    c.scan([CORPUS])
    c.backup(all=True)
    return c
