"""Path normalization and the skip lists.

Three things here are easy to get wrong and expensive to discover later:

1. **Case.** Windows paths are case-insensitive, POSIX paths are not. The index
   has a UNIQUE constraint on `path_key`, so folding case unconditionally would
   merge two genuinely different files on Linux. `path_key` is therefore
   platform-aware.

2. **Unicode.** The same filename can arrive as NFC or NFD depending on how it
   was created. Storing both forms produces phantom duplicate rows, so every
   path is normalized to NFC before it reaches the database.

3. **Extended-length prefixes.** Long Windows paths must be handed to the OS
   with an extended-length prefix, and `os.scandir` then returns children that
   inherit it. If it is not stripped again on the way back, the same file gets
   two spellings and `os.path.relpath` starts raising ValueError.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Built from separators rather than written as backslash literals, because the
# escaping of a backslash-only constant is a well-known source of silent typos.
# On Windows these are:  \\?\  and  \\?\UNC\
_EXTENDED_PREFIX = os.sep + os.sep + "?" + os.sep
_EXTENDED_UNC_PREFIX = _EXTENDED_PREFIX + "UNC" + os.sep
_UNC_ROOT = os.sep + os.sep

# Beyond this length a Windows path needs the extended-length prefix. The real
# limit is 260; the margin leaves room for a filename appended by a caller.
MAX_PATH_MARGIN = 250

# Directories skipped wholesale. Matched case-insensitively against each
# component name, so a nested `node_modules` anywhere is caught.
SKIP_DIRS = frozenset(
    {
        "$recycle.bin",
        "system volume information",
        "windows",
        "winsxs",
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        "site-packages",
    }
)

# Multi-component locations worth skipping. Matched against the portion of a
# path *below the scan root*, never the absolute path -- see walker._below.
SKIP_PATH_FRAGMENTS = (
    os.path.join("appdata", "local", "temp").lower(),
    "program files",
    "program files (x86)",
)

SKIP_FILES = frozenset({"pagefile.sys", "hiberfil.sys", "swapfile.sys", "desktop.ini", "thumbs.db"})

SKIP_SUFFIXES = frozenset({".lnk", ".tmp", ".part", ".crdownload"})


def strip_extended_prefix(p: str) -> str:
    """Remove a Windows extended-length prefix if present.

    Inverse of `long_path`, applied inside `normalize` so that every path in
    the index is stored in exactly one form.
    """
    if p.startswith(_EXTENDED_UNC_PREFIX):
        return _UNC_ROOT + p[len(_EXTENDED_UNC_PREFIX) :]
    if p.startswith(_EXTENDED_PREFIX):
        return p[len(_EXTENDED_PREFIX) :]
    return p


def normalize(p: str | os.PathLike[str]) -> str:
    """Absolute, NFC-normalized, lexically cleaned. Does not follow symlinks."""
    raw = strip_extended_prefix(os.fspath(p))
    return unicodedata.normalize("NFC", os.path.abspath(raw))


def path_key(p: str | os.PathLike[str]) -> str:
    """Identity key for a path, used as the UNIQUE column in the index.

    Case-folded on Windows only. On POSIX, `a.txt` and `A.txt` are two files
    and must stay two rows.
    """
    n = normalize(p)
    return n.casefold() if IS_WINDOWS else n


def long_path(p: str | os.PathLike[str]) -> str:
    """Return a form of `p` safe to hand to the OS when it exceeds MAX_PATH.

    The prefix also disables further path parsing, so the path must already be
    absolute and normalized -- which is why this calls `normalize` first.
    """
    n = normalize(p)
    if not IS_WINDOWS or len(n) < MAX_PATH_MARGIN:
        return n
    if n.startswith(_UNC_ROOT):  # \\server\share -> \\?\UNC\server\share
        return _EXTENDED_UNC_PREFIX + n[len(_UNC_ROOT) :]
    return _EXTENDED_PREFIX + n


def should_skip_dir(name: str) -> bool:
    return name.lower() in SKIP_DIRS


def should_skip_path(relative: str) -> bool:
    """True if this path *below a scan root* sits in a location worth skipping."""
    low = relative.lower()
    return any(frag in low for frag in SKIP_PATH_FRAGMENTS)


def should_skip_file(name: str) -> bool:
    low = name.lower()
    return low in SKIP_FILES or Path(low).suffix in SKIP_SUFFIXES


def extension(p: str | os.PathLike[str]) -> str:
    return Path(os.fspath(p)).suffix.lower()
