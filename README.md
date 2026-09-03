# Cairn

Find the documents you forgot you had, and keep the ones that matter.

Cairn is not a backup tool. Backup tools compete with Restic and Borg and lose.
Cairn is a **discovery** tool with a safety net underneath: it walks your drives,
works out which documents actually matter, makes everything searchable, and
copies the important ones into a content-addressed store.

The governing principle:

> **Store the knowledge of everything. Store the bytes of what matters.**

Cataloguing a file costs kilobytes. Keeping it costs megabytes. So Cairn
catalogues everything it can see and is selective about what it copies. A
mistake means "catalogued but not copied" — never "lost".

---

## Status: Phases 1 and 4 complete

The thin vertical slice runs end to end:

```
walk → classify → index (SQLite FTS5) → search → store → restore → verify
```

**The gate:** `cairn find tax` returns a real file, and restoring it proves the
bytes are identical. That restore test was written before any of the code it
exercises and runs first in CI.

Not yet built: PDF/OCR text extraction (Phase 2), chunking and dedup at
sub-file granularity (Phase 3), a trained classifier (Phase 5), storage budgets
(Phase 6), review UI (Phase 7).

Phase 4 runs and is tested end to end over real HTTP, but **has not been deployed
to the Pi yet** — SSH key auth from the dev machine is not set up.

---

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on POSIX
pip install -e ".[dev]"

python tools/make_fixtures.py --to fixtures/
pytest -q

cairn scan ./fixtures
cairn find tax
cairn backup --category tax
cairn restore --path ./fixtures/tax/2023_Form_1040.pdf --to ./restored
cairn verify
cairn status
```

`scan` takes explicit directories. There is no drive-wide auto-scan — you point
it at what you want indexed.

---

## Architecture

```
                   ┌──────────────┐
   directories ──▶ │   walker     │  skip lists, symlink loops, long paths,
                   │              │  cloud-placeholder guard, skip accounting
                   └──────┬───────┘
                          │ FileRecord(path, size, mtime, state)
                   ┌──────▼───────┐
                   │   extract    │  txt/md/csv direct · docx via zipfile
                   │              │  pdf/images deferred to Phase 2
                   └──────┬───────┘
                          │ body text
                   ┌──────▼───────┐
                   │   classify   │  regex rules → category, confidence, score
                   └──────┬───────┘
                          │
        ┌─────────────────┴──────────────────┐
        ▼                                    ▼
┌───────────────┐                   ┌────────────────────┐
│  SQLite index │                   │  content-addressed │
│  files + FTS5 │◀── backups ──────▶│  store             │
│  objects      │                   │  objects/ab/cd/... │
└───────────────┘                   └────────────────────┘
```

**Runtime is stdlib-only** — `sqlite3`, `hashlib`, `os`, `argparse`,
`unicodedata`, `zipfile`. Third-party packages appear only in `tools/` (fixture
generation) and `tests/`. Nothing to break when Python moves.

| Module | Responsibility |
|---|---|
| [paths.py](src/cairn/paths.py) | Normalization, case/Unicode identity, long-path prefixes, skip lists |
| [walker.py](src/cairn/walker.py) | Filesystem traversal and every platform trap |
| [extract.py](src/cairn/extract.py) | Text extraction; the seam Phase 2 widens |
| [classify.py](src/cairn/classify.py) | Rules, categories, value scoring |
| [db.py](src/cairn/db.py) | Schema, FTS5, query sanitization |
| [store.py](src/cairn/store.py) | Content-addressed object store |
| [api.py](src/cairn/api.py) | The facade the CLI and tests both drive |

---

## Design decisions

**The store is already content-addressed.** Objects live at
`store/objects/<sha[0:2]>/<sha[2:4]>/<sha>`. Phase 3 replaces a whole-file
object with a list of content-defined chunks *behind the same interface and the
same restore path*, which makes chunking a substitution rather than a rewrite.
It also means dedup works today: identical bytes cost storage once.

**Scanning does not hash.** Hashing means reading every byte on the drive, and
a scan should be the cheap operation you can run over a whole disk. Change
detection uses size + mtime; the SHA-256 is computed once, during backup, in
the same pass that copies bytes into the store.

**Writes are atomic.** Content lands in a temp file and is moved into place with
`os.replace`, so an interrupted backup can leave a stray temp file but never a
truncated object masquerading as a valid hash.

**FTS5 is synced explicitly, not by trigger.** `search.rowid` is kept equal to
`files.id`, and re-scan deletes then re-inserts. Triggers on FTS tables are a
known source of silent index drift; a test asserts the two never disagree.

**Cold ≠ unimportant, and last-access time is not evidence.** On this machine
NTFS access-time updates are enabled, but the timestamps cluster on dates when
bulk scanners swept the disk. Access time is advisory context for *which* files
to look at — never evidence of what a file is worth. Your passport scan is the
least-touched, most important file you own.

**Nothing is skipped silently.** Every exclusion is counted and printed. A scan
that quietly drops 40% of a drive is the failure mode that destroys trust.

---

## The Windows problems this actually solves

Three traps, all found by running against a real machine rather than reasoning
about them:

**Cloud placeholders.** On a OneDrive-backed profile, most files are stubs;
opening one triggers a download. Cairn checks the file attributes on the `stat`
result *before* anything opens the file, and catalogues stubs as metadata with
state `dehydrated`. Measured on a real profile:

```
scanned 2,893 files in 1.2s
  2,820 cloud placeholders catalogued as metadata (not downloaded)
```

That is **2.22 GB of downloads avoided** on one folder. A test rigs extraction
to raise if it is ever called on a placeholder, so the ordering cannot regress
unnoticed.

**Extended-length paths.** Paths over 260 characters need a `\\?\` prefix, and
`os.scandir` then returns children that *inherit* it. Left alone, the same file
gets two spellings, the UNIQUE constraint stops working, and `os.path.relpath`
raises `ValueError`. `normalize()` strips the prefix so exactly one form is ever
stored.

**Case and Unicode identity.** `path_key` folds case on Windows only — on Linux
`a.txt` and `A.txt` are two files and must stay two rows. Every path is
NFC-normalized so the same name cannot enter the index twice.

---

## Phase 4 — agent and server

The agent and the server are now separate programs. The server holds the object
store plus an aggregated catalogue across devices; the agent keeps its own local
index and pushes both bytes and metadata upstream.

```bash
# on the server (a Raspberry Pi 5 here)
bash deploy/pi-setup.sh          # installs, writes a systemd unit, prints the secret

# on the client
ssh -N -L 8823:127.0.0.1:8823 user@192.168.0.222
cairn remote enroll --url http://127.0.0.1:8823 --name win-desktop --secret <secret>
cairn scan ./fixtures
cairn push --all
cairn find tax --all-devices
cairn pull --path ./fixtures/tax/2023_Form_1040.pdf --to ./pulled
```

**The server binds to loopback on purpose.** Behind an SSH tunnel that bind *is*
the access control: the only way to reach the socket is to already be
authenticated to the host. The device token identifies which device is calling;
the tunnel authenticates the channel. When mTLS lands, the client certificate
subject replaces the token lookup and nothing else changes.

Why HTTP rather than plain SSH transport: the server is a *query* service, not a
blob sink. "Which of these 400 digests do you already have?" is one round trip;
over SSH it would be a process spawn per query or a hand-rolled RPC inside an SSH
channel. Per-device authorization is also row-level policy, which a Unix account
cannot express — and Phase 6's cloud backend and Phase 7's web UI both speak HTTP
already.

### Three properties with tests behind them

**Uploads are re-hashed server-side.** The URL claims a digest; the server hashes
the bytes that actually arrive and returns 409 on a mismatch. Storing bytes under
the wrong name poisons a content-addressed store permanently — every later dedup
hit returns wrong content, and `verify` cannot detect it because it compares
against the same wrong name.

**Knowing a digest is not authorization to read it.** Downloads are checked
against `object_refs` scoped to visible devices. Refusals return 404, not 403, so
the response cannot be used to probe which digests other devices hold.

**Dedup must not cost ownership.** This one was a real bug, caught by running the
two-device demo rather than by a test. When the server already held identical
bytes, the second device's push skipped the transfer *and* never claimed a
reference — so the push reported success while leaving that device unable to
retrieve its own file. Silent false success is the worst failure mode a tool like
this has. Fixed with an explicit `/objects/claim`, and pinned by a regression test.

### Measured, two devices against one server

```
win-desktop  push --all    36 uploaded,  4 already present   170.9 KB sent
win-desktop  push --all     0 uploaded, 40 already present     0 B sent   (100% skipped)
pi-agent     push --all     0 uploaded,  2 already present     0 B sent   (cross-device dedup)
pi-agent     pull                        byte-for-byte identical
pi-agent     find tax --all-devices      no matches           (isolation holds)
win-desktop  find tax --all-devices      3 hits from win-desktop
```

The second push moving zero bytes is resumability and deduplication being the
same mechanism: ask what the server has, send only the difference.

---

## Results on real documents

Scanning ~5,800 real files, the highest-value documents surfaced were:

```
0.94  identity    Birth Certificate.pdf          [dehydrated]
0.75  education   Official Transcript.pdf        [dehydrated]
0.71  employment  Resume.pdf                     [dehydrated]
```

The top-ranked document is a birth certificate that **was never downloaded** —
catalogued, scored, and searchable while its bytes stayed in the cloud. That is
the thesis working.

Running against real data also found three genuine classifier bugs that the
synthetic corpus could never have caught:

| Bug | Effect | Fix |
|---|---|---|
| `master` matched git branch names in `.gitignore` | 356 files misfiled as `education` | require `master of` / `master's degree` |
| `\bmd\b` matched the Markdown extension | 20 files pulled toward `medical` | require `m.d.` |
| MIT/Apache `LICENSE` files read as legal contracts | 31 of 39 `legal` hits | explicit software-license demotion rules |

After the fixes: `education` 432 → 76, `legal` 39 → 8, software licenses 31 → 0.

Known false positives that remain, listed rather than tuned away:
`Homework 5-Routing.pdf` scores as `financial` (matches "routing"), and some
coursework `.docx` files score as `legal`. Both are Phase 5's problem.

---

## The rules baseline, and why you should not trust it

`pytest` prints a precision/recall table and writes [metrics.json](metrics.json).
On the fixture corpus the rules score **100%** — with and without the folder-name
signal.

**That number is meaningless.** The fixture document bodies and the regex rules
were written by the same hand, so the rules match vocabulary they were built
against. A benchmark that cannot falsify the thing it measures is not a
benchmark. The caveat ships inside `metrics.json` so the figure can never be
quoted without it.

The real-corpus results above are far more informative, and they are exactly why
Phase 5 builds a hand-labelled set from real documents. Whatever the trained
classifier scores against *that*, the honest comparison goes here — including if
the rules win.

---

## Tests

98 tests. Verified on Windows against Python 3.14. The CI matrix also covers
`ubuntu-latest` and Python 3.12, but has not run yet — there is no remote, so
those legs are configured, not observed.

| Module | Covers |
|---|---|
| [test_restore_roundtrip.py](tests/test_restore_roundtrip.py) | The gate: every file restores byte-for-byte, parametrized per fixture |
| [test_walker.py](tests/test_walker.py) | Skip lists, symlink loops, long/Unicode paths, placeholder safety |
| [test_index.py](tests/test_index.py) | Search, hostile FTS queries, re-scan idempotence, dedup, refcounts |
| [test_classify.py](tests/test_classify.py) | Precision/recall baseline with folder-signal ablation |

The corpus is generated by [tools/make_fixtures.py](tools/make_fixtures.py):
~50 documents with ground-truth labels, plus deliberate edge cases — a zero-byte
file, no extension, `Ünïcödé_résumé.pdf`, a CJK filename, a 356-character path,
and two files with byte-identical content to prove dedup.
