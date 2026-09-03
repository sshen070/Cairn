"""Storage backends: the seam between "where bytes live" and everything else.

Phase 1 had exactly one place bytes could go. Phase 4 adds a second, and Phase 6
adds a third (cloud). All three answer the same five questions, so they share an
interface and the rest of the codebase never learns which one it is holding.

The interface is deliberately **hash-first**. `Store.put(src)` computes the digest
itself, which is right for a local copy but wrong across a network: the agent must
know the hash *before* it uploads, so it can ask the server what it already has and
skip those. `has_many` is that question, and it is the whole of resumable transfer
-- interrupt an upload, run it again, and only the missing objects move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .store import Store, hash_file


@runtime_checkable
class StorageBackend(Protocol):
    """Where a content-addressed object can be put, fetched, and checked."""

    def has(self, sha256: str) -> bool: ...

    def has_many(self, digests: list[str]) -> set[str]:
        """Which of these digests are already stored. One round trip, not N."""
        ...

    def put(self, digest: str, src: Path) -> bool:
        """Store the bytes of `src` under `digest`. Returns False if already present."""
        ...

    def get(self, digest: str, dest: Path) -> Path: ...

    def verify(self, digest: str) -> bool: ...


class LocalBackend:
    """The Phase 1 store, wearing the backend interface."""

    def __init__(self, store: Store):
        self.store = store
        self.store.initialize()

    @property
    def label(self) -> str:
        return f"local:{self.store.root}"

    def has(self, digest: str) -> bool:
        return self.store.exists(digest)

    def has_many(self, digests: list[str]) -> set[str]:
        return {d for d in digests if self.store.exists(d)}

    def put(self, digest: str, src: Path) -> bool:
        result = self.store.put(src)
        if result.sha256 != digest:
            raise ValueError(
                f"digest mismatch storing {src}: caller said {digest[:12]}, bytes hash to {result.sha256[:12]}"
            )
        return not result.deduplicated

    def get(self, digest: str, dest: Path) -> Path:
        return self.store.extract_to(digest, dest)

    def verify(self, digest: str) -> bool:
        return self.store.verify(digest)


class RemoteBackend:
    """A Cairn server reached over HTTP. Same five questions, different machine."""

    def __init__(self, client):  # cairn.client.CairnClient
        self.client = client

    @property
    def label(self) -> str:
        return f"remote:{self.client.config.url}"

    def has(self, digest: str) -> bool:
        return bool(self.has_many([digest]))

    def has_many(self, digests: list[str]) -> set[str]:
        return self.client.have(digests)

    def put(self, digest: str, src: Path) -> bool:
        result = self.client.put_object(digest, src)
        return bool(result.get("stored"))

    def get(self, digest: str, dest: Path) -> Path:
        return self.client.get_object(digest, dest)

    def verify(self, digest: str) -> bool:
        """Only the server can rehash its own bytes; this asks whether it has them.

        A real integrity check of remote content means downloading it, which
        `Cairn.pull` does and re-verifies locally.
        """
        return self.has(digest)


def digest_of(path: str | Path) -> str:
    """Hash a file without storing it -- what the agent does before asking the server."""
    return hash_file(path)
