"""Content-addressed object store.

Phase 1 stores whole files keyed by SHA-256, sharded two levels deep:

    store/objects/ab/cd/abcd...ef

Phase 3 replaces the whole-file object with a list of content-defined chunks.
Crucially it does so *behind this interface* -- `put`/`get`/`verify` and the
restore path stay as they are, which makes chunking a substitution rather than
a rewrite. Whole-file addressing also means dedup already works today: two
files with identical bytes occupy one object.

Writes are atomic. Content lands in a temp file inside the store and is moved
into place with `os.replace`, so an interrupted backup can leave a stray temp
file but never a truncated object masquerading as a valid hash.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import paths

READ_BLOCK = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class PutResult:
    sha256: str
    size: int
    deduplicated: bool
    """True when the object was already present and no bytes were written."""


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.tmp = self.root / "tmp"

    def initialize(self) -> None:
        self.objects.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha256: str) -> Path:
        if len(sha256) != 64:
            raise ValueError(f"not a sha256 digest: {sha256!r}")
        return self.objects / sha256[:2] / sha256[2:4] / sha256

    def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    def put(self, src: str | Path) -> PutResult:
        """Copy `src` into the store, returning its digest.

        Hash and copy happen in a single pass: the source is read once, which
        matters when the source is a spinning disk or a network share.
        """
        self.initialize()
        digest = hashlib.sha256()
        size = 0

        fd, tmp_name = tempfile.mkstemp(dir=self.tmp, prefix="put-")
        tmp_path = Path(tmp_name)
        try:
            with open(paths.long_path(src), "rb") as fin, os.fdopen(fd, "wb") as fout:
                while block := fin.read(READ_BLOCK):
                    digest.update(block)
                    fout.write(block)
                    size += len(block)

            sha = digest.hexdigest()
            dest = self.path_for(sha)
            if dest.exists():
                tmp_path.unlink(missing_ok=True)
                return PutResult(sha, size, deduplicated=True)

            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, dest)
            return PutResult(sha, size, deduplicated=False)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def get(self, sha256: str) -> Path:
        p = self.path_for(sha256)
        if not p.exists():
            raise KeyError(f"object not in store: {sha256}")
        return p

    def extract_to(self, sha256: str, dest: str | Path) -> Path:
        """Copy the object out of the store to `dest`."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.get(sha256), paths.long_path(dest))
        return dest

    def verify(self, sha256: str) -> bool:
        """Rehash the stored object and confirm it still matches its name."""
        try:
            p = self.get(sha256)
        except KeyError:
            return False
        return hash_file(p) == sha256

    def object_count(self) -> int:
        if not self.objects.exists():
            return 0
        return sum(1 for _ in self.objects.rglob("*") if _.is_file())

    def total_bytes(self) -> int:
        if not self.objects.exists():
            return 0
        return sum(p.stat().st_size for p in self.objects.rglob("*") if p.is_file())


def hash_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(paths.long_path(path), "rb") as fh:
        while block := fh.read(READ_BLOCK):
            digest.update(block)
    return digest.hexdigest()
