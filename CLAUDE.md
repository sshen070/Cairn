# Cairn — working notes for Claude

Cairn finds documents the user has forgotten about across their drives, decides which
ones matter, indexes everything so it is searchable, and copies the important ones into
a content-addressed store.

**It is not a backup tool.** Backup tools compete with Restic and Borg and lose. Cairn is
a *discovery* tool with a safety net underneath. When a design choice is ambiguous,
resolve it toward "help the user find something they forgot they had", not toward
"maximize bytes preserved".

The governing principle, which most of the architecture follows from:

> **Store the knowledge of everything. Store the bytes of what matters.**

Cataloguing costs kilobytes; keeping costs megabytes. A wrong call must mean
*catalogued but not copied*, never *lost*.

---

## Environment

- Windows 11 dev machine, Python **3.14.6** (the only interpreter installed; no `py` launcher).
- Target server is a **Raspberry Pi 5** (Linux, ARM64) that the user owns. Cloud comes later.
- Use `.venv/Scripts/python.exe` — the CLI entry point is `cairn`, installed via `pip install -e ".[dev]"`.
- The Bash tool is available but **mangles backslash escapes in heredocs**. For any edit
  involving `\` (regex, Windows paths), use the Edit/Write tools instead.
- `pkill` does not reach these Python processes. To stop a running server use
  `Get-CimInstance Win32_Process ... | Stop-Process` via the PowerShell tool, or a stale
  process keeps serving old code and you will debug phantom 404s and 405s.
- Server target is **192.168.0.222** (`pi@`), password auth only — no key installed, so
  the agent cannot open the tunnel unattended. `JayPi5` resolves over mDNS to IPv6; prefer
  the IP so the tunnel stays on the IPv4 LAN.
- **The Pi runs whatever was last deployed.** After changing anything under `src/cairn/server/`,
  re-run `deploy/push-to-pi.sh` or you are testing new client code against an old server.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe tools/make_fixtures.py --to fixtures --clean
.venv/Scripts/python.exe -m pytest -q
```

---

## Layout

```
src/cairn/
  paths.py      normalization, case/Unicode identity, long-path prefixes, skip lists
  walker.py     traversal + every platform trap; yields FileRecord + SkipSummary
  extract.py    text extraction; the seam Phase 2 widens
  classify.py   RULES / DEMOTIONS / FOLDER_SIGNAL, category + confidence + value score
  db.py         schema, FTS5, fts_quote()
  store.py      content-addressed object store
  api.py        Cairn facade — the CLI and the tests both drive this, not the modules
  cli.py        argparse; scan / find / backup / restore / verify / status
                + remote enroll|status|diff|verify, push, pull
  backend.py    StorageBackend protocol; LocalBackend / RemoteBackend
  client.py     stdlib HTTP client for the agent (urllib + ssl)
  server/       FastAPI app, server schema — the only place third-party deps live
tools/make_fixtures.py   dev-only corpus generator (reportlab, python-docx)
tests/                   conftest builds the corpus once per session
```

`Cairn` in `api.py` is the seam. Add behaviour there rather than wiring the CLI directly
into `walker`/`store`, so tests keep exercising the same path the CLI does.

---

## Invariants — do not break these

**1. The cloud-placeholder guard runs before any `open()`.**
On this user's machine ~99% of OneDrive files are stubs; opening one triggers a download.
`walker.is_dehydrated(st)` is checked against the `stat` result, before reading. Measured:
2,820 placeholders catalogued, **2.22 GB of downloads avoided**. Moving this check below a
read silently disables it while every test still passes — which is why a test rigs
`extract_text` to raise if it is ever called on a placeholder.

**2. Runtime is stdlib-only.** `sqlite3`, `hashlib`, `os`, `argparse`, `unicodedata`,
`zipfile`. Third-party packages appear only in `tools/` and `tests/`. If a phase needs a
dependency (Phase 2 needs pypdf/Tesseract; Phase 4 needs FastAPI **server-side**), keep it
out of the agent's import path where possible.

**3. Paths are stored in exactly one form.** `normalize()` makes them absolute, NFC, and
strips the Windows `\\?\` extended-length prefix — `os.scandir` on a long path returns
children that *inherit* that prefix, which otherwise produces two spellings of one file.
`path_key()` case-folds **on Windows only**; on Linux `a.txt` and `A.txt` are two files.

**4. Skip lists yield to explicit intent.** Skip fragments match the path *below the scan
root*, never the absolute path. A directory named on the command line is always scanned.
(Matching absolute paths was a real bug: any corpus under `AppData\Local\Temp` vanished.)

**5. Nothing is skipped silently.** Every exclusion is counted by reason in `SkipSummary`
and printed after each scan.

**6. FTS5 is synced by hand, not by trigger.** `search.rowid == files.id`; re-scan deletes
then re-inserts. A test asserts the row counts never diverge.

**7. Scanning does not hash.** Change detection uses `size` + `mtime_ns` + `state`. The
SHA-256 is computed once, during backup, in the same pass that copies bytes. Keeps `scan`
proportional to file *count*, not total size (2,893 files in 1.2 s).

**8. Store writes are atomic.** Temp file inside the store, then `os.replace`. An
interrupted backup may leave a stray temp file but never a truncated object under a valid
hash.

**9. The server re-hashes every upload.** The URL claims a digest; the server computes the
digest of the bytes that arrive and returns 409 on a mismatch. Storing bytes under the
wrong name poisons a content-addressed store permanently — every later dedup hit returns
wrong content and `verify` cannot tell, because it compares against the same wrong name.
Never trust a client's arithmetic about content it supplies.

**10. Knowing a digest is not authorization to read it.** Downloads are ACL-checked against
`object_refs` scoped to visible devices, and refusals return 404 rather than 403 so the
response cannot be used to probe which digests other devices hold.

**11. Dedup must not cost ownership.** When the server already holds identical bytes, the
pushing device still calls `/objects/claim` to register a reference. Skipping that made a
push report success while leaving the device unable to retrieve its own file — silent false
success, which is the worst failure mode this project has.

**12. A digest is only valid for the bytes it was computed from.** `_index()` sets
`files.sha256 = NULL` whenever a re-scan sees a changed file. Without that, `push` announced
a stale hash while uploading new bytes, and only the server's 409 caught it — 11 of 28 files
on the first real deployment. Text fixtures hid this for weeks because they regenerate
byte-identically; PDF and DOCX embed a creation timestamp and do not.

**13. `backups` is a local archive log, not a statement about the server.** `pull` resolves a
path through `files.sha256` (current content) and only falls back to `backups` (history).
Reversing that asks the server for an object it was never sent.

**14. A cached claim about a remote is only true while the digest matches.**
`remote_state` records what a server confirmed it holds. A row counts as backed up only
while `remote_state.sha256` still equals the file's current `files.sha256`, so editing a
file expires the claim on its own. `remote diff --refresh` covers the other direction,
which the cache cannot see: objects pruned on the server.

---

## Conventions

- Comments explain **why**, not what. Prefer one good comment on a non-obvious decision
  over narration of the next line.
- Rules in `classify.py` are *data*. Add regexes to `RULES`/`DEMOTIONS`, do not add
  branching logic.
- Tests are an independent statement of intent — e.g. `tests/conftest.py` re-declares the
  skip-list directory names rather than importing them, so a production change fails loudly.
- Write the test before the feature for anything on the restore path.
- Never invent metrics. Every number in the README was measured; if a claim cannot be run,
  say it is unverified.

---

## Honest state — do not overstate these

- **Only Windows / Python 3.14 is verified.** The CI matrix covers `ubuntu-latest` and
  3.12 as well, but there is no remote yet, so **CI has never run**. Do not describe Linux
  as passing.
- **The rules benchmark is saturated.** 100% on the fixture corpus, because the fixture
  bodies and the regexes share an author. `metrics.json` carries the caveat inline. Real
  measurement needs the hand-labelled set Phase 5 builds. Never quote the 100%.
- **The store is self-verifying but not self-describing.** Object filenames are their own
  digests, but the path→hash mapping lives only in `backups` in `cairn.db`. Lose the
  database and you keep every byte and no filenames. A manifest object written into the
  store is a Phase 3 task, not polish.
- Known classifier false positives, documented rather than tuned away:
  `Homework 5-Routing.pdf` → `financial` (matches "routing"); some coursework → `legal`.

---

## Phases

| Phase | Status | Notes |
|---|---|---|
| 1 — thin vertical slice | **done** | 98 tests; walk → classify → FTS5 → search → store → restore → verify |
| 4 — client/server | **done (localhost)** | 13 tests; enroll → push → pull, per-device scope. Not yet deployed to the Pi |
| 2 — content extraction | next | pypdf + Tesseract; fills the `extract.py` seam. Needs ARM64 builds for the Pi |
| 3 — chunked store | later | FastCDC behind `store.put`; `objects.refcount` already exists for GC |
| 5 — valuation v2 | later | TF-IDF + logistic regression vs the rules, on real labelled data |
| 6 — budget and tiering | later | Acts on the selection step; client-side encryption before anything leaves the LAN |
| 7 — human in the loop | later | pin / exclude / confirm, folded back into `score` |

Each later phase attaches to a seam that already exists. Prefer substitution behind an
existing interface over reshaping the stages above it.
