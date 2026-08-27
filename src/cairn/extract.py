"""Text extraction. Phase 1 scope, and the seam Phase 2 widens.

Phase 1 deliberately handles only what the standard library can read:

  - plain text families, decoded defensively
  - DOCX, which is a zip of XML and costs about fifteen lines

PDF is *not* handled here. It needs pypdf, and scanned PDFs need OCR, which is
the whole of Phase 2. Until then a PDF is indexed by its filename and path,
which is enough for `cairn find tax` to return `2023_Form_1040.pdf`.

`extract_text` never raises: a document that cannot be read still deserves a
row in the index built from its name.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from . import paths

# Text-ish formats safe to read directly.
PLAIN_TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".yaml", ".yml", ".ini", ".cfg", ".rst"}
)

# Formats whose text arrives in Phase 2. Listed so `status` can report how much
# content the index is currently missing rather than pretending it has it all.
DEFERRED_EXTENSIONS = frozenset({".pdf", ".doc", ".rtf", ".odt", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".tiff"})

MAX_TEXT_BYTES = 1 << 20  # 1 MiB of text is far more than classification needs.

_XML_TAG = re.compile(rb"<[^>]+>")
_PARAGRAPH_BREAK = re.compile(rb"</w:p>")
_WS = re.compile(r"[ \t\r\f\v]+")


def extract_text(path: str | Path, ext: str | None = None) -> str:
    """Best-effort text for `path`. Returns '' when the format is deferred."""
    p = Path(path)
    ext = (ext or paths.extension(p)).lower()

    try:
        if ext in PLAIN_TEXT_EXTENSIONS:
            return _read_plain(p)
        if ext == ".docx":
            return _read_docx(p)
    except (OSError, zipfile.BadZipFile, UnicodeError):
        return ""
    return ""


def _read_plain(p: Path) -> str:
    with open(paths.long_path(p), "rb") as fh:
        raw = fh.read(MAX_TEXT_BYTES)
    return _tidy(raw.decode("utf-8", errors="replace"))


def _read_docx(p: Path) -> str:
    """Pull the text out of word/document.xml without a third-party parser."""
    with zipfile.ZipFile(paths.long_path(p)) as zf:
        try:
            xml = zf.read("word/document.xml")
        except KeyError:
            return ""
    # Turn paragraph ends into newlines first, or every word runs together.
    xml = _PARAGRAPH_BREAK.sub(b"\n", xml)
    text = _XML_TAG.sub(b" ", xml).decode("utf-8", errors="replace")
    return _tidy(_unescape(text))


def _unescape(s: str) -> str:
    return (
        s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    )


def _tidy(s: str) -> str:
    lines = (_WS.sub(" ", line).strip() for line in s.splitlines())
    return "\n".join(line for line in lines if line)


def name_tokens(path: str | Path) -> str:
    """Filename split into searchable words.

    `2023_Form_1040.pdf` should be findable by `1040` and by `form`, so the
    separators become spaces and the original is kept alongside.
    """
    stem = Path(path).stem
    return f"{stem} {re.sub(r'[_\-.]+', ' ', stem)}".strip()


def path_tokens(path: str | Path) -> str:
    """Directory components as searchable words, filename excluded."""
    parts = Path(path).parent.parts
    cleaned = [re.sub(r"[_\-]+", " ", part) for part in parts if not _is_noise_component(part)]
    return " ".join(cleaned)


def _is_noise_component(part: str) -> bool:
    low = part.rstrip("\\/").lower()
    return low.endswith(":") or low in {"", "\\", "/", "users", "home", "c:"}
