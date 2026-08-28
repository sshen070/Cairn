"""Command line interface. argparse, because the runtime takes no dependencies."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__, classify
from .api import Cairn

DEFAULT_DB = Path(os.environ.get("CAIRN_DB", "cairn.db"))
DEFAULT_STORE = Path(os.environ.get("CAIRN_STORE", "store"))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def when(ns: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ns / 1e9))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cairn",
        description="Find the documents you forgot you had, and keep the ones that matter.",
    )
    p.add_argument("--version", action="version", version=f"cairn {__version__}")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"index database (default: {DEFAULT_DB})")
    p.add_argument("--store", type=Path, default=DEFAULT_STORE, help=f"object store (default: {DEFAULT_STORE})")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the index and store")

    sc = sub.add_parser("scan", help="walk directories and index what is found")
    sc.add_argument("paths", nargs="+", type=Path, help="directories to scan")
    sc.add_argument(
        "--hydrate",
        action="store_true",
        help="download cloud placeholders instead of cataloguing them as metadata "
        "(this can pull a very large amount of data)",
    )
    sc.add_argument("--max-size", type=float, default=None, metavar="MB", help="skip files larger than this")

    fd = sub.add_parser("find", help="full-text search the index")
    fd.add_argument("query", nargs="+")
    fd.add_argument("--limit", type=int, default=20)
    fd.add_argument("--category", choices=classify.CATEGORIES)

    bk = sub.add_parser("backup", help="copy selected files into the store")
    bk.add_argument("--all", action="store_true", help="every readable indexed file")
    bk.add_argument("--query", help="everything matching this search")
    bk.add_argument("--category", choices=classify.CATEGORIES)
    bk.add_argument("--min-score", type=float, metavar="S")
    bk.add_argument("--limit", type=int)
    bk.add_argument("--dry-run", action="store_true", help="report the selection without copying")

    rs = sub.add_parser("restore", help="write a backed-up file back out")
    g = rs.add_mutually_exclusive_group(required=True)
    g.add_argument("--sha", help="restore by object digest")
    g.add_argument("--path", type=Path, help="restore by original path")
    rs.add_argument("--to", type=Path, required=True, help="destination directory")

    vf = sub.add_parser("verify", help="rehash stored objects and check them")
    vf.add_argument("--sha", help="check a single object")

    sub.add_parser("status", help="summarize the index and store")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cairn = Cairn(db_path=args.db, store_path=args.store)
    try:
        return _dispatch(args, cairn)
    finally:
        cairn.close()


def _dispatch(args: argparse.Namespace, cairn: Cairn) -> int:
    if args.command == "init":
        print(f"index  {args.db.resolve()}")
        print(f"store  {args.store.resolve()}")
        return 0

    if args.command == "scan":
        return cmd_scan(args, cairn)
    if args.command == "find":
        return cmd_find(args, cairn)
    if args.command == "backup":
        return cmd_backup(args, cairn)
    if args.command == "restore":
        return cmd_restore(args, cairn)
    if args.command == "verify":
        return cmd_verify(args, cairn)
    if args.command == "status":
        return cmd_status(cairn)
    return 2


def cmd_scan(args: argparse.Namespace, cairn: Cairn) -> int:
    max_size = int(args.max_size * 1024 * 1024) if args.max_size else None
    try:
        r = cairn.scan(args.paths, hydrate=args.hydrate, max_size=max_size)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"scanned {r.seen:,} files in {r.elapsed:.1f}s")
    print(f"  {r.added:,} added   {r.updated:,} updated   {r.unchanged:,} unchanged")
    if r.dehydrated:
        print(f"  {r.dehydrated:,} cloud placeholders catalogued as metadata (not downloaded)")

    # Always print the skip accounting. A scan that silently drops files is
    # worse than one that reports an ugly number.
    print("skipped:")
    print(r.skips.render())
    return 0


def cmd_find(args: argparse.Namespace, cairn: Cairn) -> int:
    hits = cairn.find(" ".join(args.query), limit=args.limit, category=args.category)
    if not hits:
        print("no matches")
        return 1

    width = max(len(h.category or "") for h in hits)
    for i, h in enumerate(hits, 1):
        flag = "" if h.state == "indexed" else f" [{h.state}]"
        print(f"{i:>3}. {h.category or '-':<{width}}  {h.score:>5.2f}  {human(h.size):>9}  {when(h.mtime_ns)}  {h.path}{flag}")
        if h.snippet:
            print(f"      {h.snippet}")
    return 0


def cmd_backup(args: argparse.Namespace, cairn: Cairn) -> int:
    try:
        r = cairn.backup(
            all=args.all, query=args.query, category=args.category,
            min_score=args.min_score, limit=args.limit, dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if r.dry_run:
        print(f"would back up {r.considered:,} files, {human(r.logical_bytes)}")
        return 0

    print(f"considered {r.considered:,} files")
    print(f"  {r.stored:,} newly recorded   {r.already_backed_up:,} already present")
    print(f"  {r.deduplicated:,} matched an object already in the store")
    print(f"  {human(r.logical_bytes)} logical -> {human(r.physical_bytes)} written")
    if r.failed:
        print(f"  {len(r.failed)} failed:", file=sys.stderr)
        for path, why in r.failed[:10]:
            print(f"    {path}: {why}", file=sys.stderr)
        return 1
    return 0


def cmd_restore(args: argparse.Namespace, cairn: Cairn) -> int:
    try:
        out = cairn.restore(sha=args.sha, path=args.path, to=args.to)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"restored {out}")
    return 0


def cmd_verify(args: argparse.Namespace, cairn: Cairn) -> int:
    r = cairn.verify(sha=args.sha)
    print(f"checked {r.checked:,} objects")
    if r.missing:
        print(f"  MISSING  {len(r.missing)}", file=sys.stderr)
        for d in r.missing[:10]:
            print(f"    {d}", file=sys.stderr)
    if r.corrupt:
        print(f"  CORRUPT  {len(r.corrupt)}", file=sys.stderr)
        for d in r.corrupt[:10]:
            print(f"    {d}", file=sys.stderr)
    if r.missing or r.corrupt:
        return 1
    print("  all objects intact")
    return 0


def cmd_status(cairn: Cairn) -> int:
    s = cairn.status()

    print("index")
    for state, n in sorted(s["by_state"].items()):
        print(f"  {n:>7,}  {state}")
    if s["awaiting_extraction"]:
        print(f"  {s['awaiting_extraction']:>7,}  indexed by filename only (PDF/image text lands in Phase 2)")

    if s["by_category"]:
        print("categories")
        for cat, n in s["by_category"].items():
            print(f"  {n:>7,}  {cat}")

    print("store")
    print(f"  {s['objects']:>7,}  objects  ({human(s['physical_bytes'])} on disk)")
    print(f"  {s['backups']:>7,}  backed-up files  ({human(s['logical_bytes'])} logical)")
    print(f"  dedup ratio {s['dedup_ratio']:.2f}x  ({human(s['bytes_saved'])} saved)")

    last = s["last_scan"]
    if last and last.get("skips"):
        print("last scan skipped")
        for reason, n in sorted(last["skips"].items(), key=lambda kv: -kv[1]):
            print(f"  {n:>7,}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
