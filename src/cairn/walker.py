"""Filesystem walk, with the platform traps handled explicitly.

The single most important rule in this module: **decide whether a file is a
cloud placeholder before opening it.** On a machine with OneDrive, most files
under the user profile are stubs; opening one triggers a download. A scanner
that hashes indiscriminately will pull the entire cloud account onto local
disk. So the attribute check happens on the `stat` result, before any read.

Second rule: never skip silently. Every exclusion lands in a `SkipSummary` that
the CLI prints after each scan. A scan that quietly drops 40% of a drive is the
failure mode that makes the whole tool untrustworthy.
"""

from __future__ import annotations

import os
import stat as stat_mod
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import paths

# Windows file attributes. Present in os.stat_result.st_file_attributes on Win32.
FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

_DEHYDRATED_MASK = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

# States a row can hold in the index.
INDEXED = "indexed"
DEHYDRATED = "dehydrated"
SKIPPED = "skipped"
ERROR = "error"


@dataclass(frozen=True)
class FileRecord:
    path: str
    size: int
    mtime_ns: int
    atime_ns: int
    ext: str
    state: str
    skip_reason: str | None = None

    @property
    def readable(self) -> bool:
        """Whether the file's bytes may be opened."""
        return self.state == INDEXED


@dataclass
class SkipSummary:
    reasons: Counter = field(default_factory=Counter)
    dirs_pruned: Counter = field(default_factory=Counter)

    def note(self, reason: str) -> None:
        self.reasons[reason] += 1

    def prune(self, reason: str) -> None:
        self.dirs_pruned[reason] += 1

    @property
    def total(self) -> int:
        return sum(self.reasons.values())

    def render(self) -> str:
        if not self.reasons and not self.dirs_pruned:
            return "  nothing skipped"
        lines = []
        for reason, n in self.reasons.most_common():
            lines.append(f"  {n:>7,}  {reason}")
        for reason, n in self.dirs_pruned.most_common():
            lines.append(f"  {n:>7,}  directories pruned: {reason}")
        return "\n".join(lines)


def is_dehydrated(st: os.stat_result) -> bool:
    """True if reading this file would pull it down from cloud storage."""
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & _DEHYDRATED_MASK)


def walk(
    roots: list[Path] | list[str],
    *,
    hydrate: bool = False,
    max_size: int | None = None,
    follow_symlinks: bool = False,
) -> tuple[list[FileRecord], SkipSummary]:
    """Walk `roots`, returning the records found and an accounting of exclusions.

    `hydrate=False` (the default) catalogues cloud placeholders as metadata
    without opening them. `max_size` is in bytes; larger files are catalogued
    but not marked readable.
    """
    summary = SkipSummary()
    records: list[FileRecord] = []
    visited: set[tuple[int, int]] = set()

    for root in roots:
        root_path = paths.normalize(root)
        if not os.path.exists(paths.long_path(root_path)):
            raise FileNotFoundError(f"scan root does not exist: {root_path}")
        records.extend(_walk_one(root_path, summary, visited, hydrate, max_size, follow_symlinks))

    return records, summary


def _below(root: str, path: str) -> str:
    """The portion of `path` beneath `root`, for skip-list matching."""
    try:
        return os.path.relpath(paths.normalize(path), paths.normalize(root))
    except ValueError:  # genuinely different drives on Windows
        return os.path.basename(path)


def _walk_one(
    root: str,
    summary: SkipSummary,
    visited: set[tuple[int, int]],
    hydrate: bool,
    max_size: int | None,
    follow_symlinks: bool,
) -> Iterator[FileRecord]:
    stack: list[str] = [root]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(paths.long_path(current)) as it:
                entries = list(it)
        except PermissionError:
            summary.note("permission denied (directory)")
            continue
        except OSError as exc:
            summary.note(f"unreadable directory ({exc.__class__.__name__})")
            continue

        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if paths.should_skip_dir(entry.name):
                        summary.prune(entry.name.lower())
                        continue
                    # Judge only the part of the path below the scan root. An
                    # explicitly requested root is always honoured -- the skip
                    # lists exist to stop a broad walk wandering into system
                    # locations, not to veto a direct request to scan one.
                    if paths.should_skip_path(_below(root, entry.path)):
                        summary.prune("system location")
                        continue
                    stack.append(entry.path)
                    continue

                if entry.is_symlink() and not follow_symlinks:
                    summary.note("symlink not followed")
                    continue

                if not entry.is_file(follow_symlinks=follow_symlinks):
                    summary.note("not a regular file")
                    continue

                if paths.should_skip_file(entry.name):
                    summary.note("excluded filename")
                    continue

                rec = _record_for(entry, visited, summary, hydrate, max_size)
                if rec is not None:
                    yield rec

            except PermissionError:
                summary.note("permission denied (file)")
            except OSError as exc:
                summary.note(f"stat failed ({exc.__class__.__name__})")


def _record_for(
    entry: os.DirEntry[str],
    visited: set[tuple[int, int]],
    summary: SkipSummary,
    hydrate: bool,
    max_size: int | None,
) -> FileRecord | None:
    st = entry.stat(follow_symlinks=False)

    # Hardlink / loop guard: the same inode reached by two names is one file.
    ident = (st.st_dev, st.st_ino)
    if st.st_ino and ident in visited:
        summary.note("already seen (hardlink or loop)")
        return None
    if st.st_ino:
        visited.add(ident)

    norm = paths.normalize(entry.path)
    ext = paths.extension(entry.name)
    common = dict(path=norm, size=st.st_size, mtime_ns=st.st_mtime_ns, atime_ns=st.st_atime_ns, ext=ext)

    # Placeholder check happens here, on the stat result, before anything opens
    # the file. Reordering this below a read would be a serious regression.
    if is_dehydrated(st) and not hydrate:
        summary.note("cloud placeholder (not downloaded)")
        return FileRecord(**common, state=DEHYDRATED, skip_reason="cloud placeholder")

    if not stat_mod.S_ISREG(st.st_mode):
        summary.note("not a regular file")
        return FileRecord(**common, state=SKIPPED, skip_reason="not a regular file")

    if max_size is not None and st.st_size > max_size:
        summary.note("larger than max-size")
        return FileRecord(**common, state=SKIPPED, skip_reason="exceeds max-size")

    return FileRecord(**common, state=INDEXED)
