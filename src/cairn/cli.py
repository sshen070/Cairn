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
    fd.add_argument("--all-devices", action="store_true",
                    help="search the server's catalogue across every device you can see")
    fd.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)

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

    # -- Phase 4: remote ----------------------------------------------------

    rm = sub.add_parser("remote", help="enroll with, or inspect, a Cairn server")
    rmsub = rm.add_subparsers(dest="remote_command", required=True)

    en = rmsub.add_parser("enroll", help="exchange the enrollment secret for a device token")
    en.add_argument("--url", required=True, help="e.g. http://127.0.0.1:8823 through an SSH tunnel")
    en.add_argument("--name", required=True, help="device name, unique per server")
    en.add_argument("--secret", required=True, help="printed by the server at startup")
    en.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (self-signed cert during bring-up only)")

    st = rmsub.add_parser("status", help="show the configured server and what it can see")
    st.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)

    df = rmsub.add_parser("diff", help="which readable files are not on the server")
    df.add_argument("--refresh", action="store_true",
                    help="ask the server what it actually holds instead of trusting the local cache")
    df.add_argument("--limit", type=int, default=15, help="how many missing files to list")
    df.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)

    rv = rmsub.add_parser("verify", help="have the server re-hash its stored bytes")
    rv.add_argument("--all", action="store_true",
                    help="check every object this device can see, not just its own")
    rv.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)

    ph = sub.add_parser("push", help="send selected files and their metadata to the server")
    ph.add_argument("--all", action="store_true")
    ph.add_argument("--query")
    ph.add_argument("--category", choices=classify.CATEGORIES)
    ph.add_argument("--min-score", type=float, metavar="S")
    ph.add_argument("--limit", type=int)
    ph.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)

    pl = sub.add_parser("pull", help="fetch a file back from the server and verify it")
    pg = pl.add_mutually_exclusive_group(required=True)
    pg.add_argument("--sha")
    pg.add_argument("--path", type=Path)
    pl.add_argument("--to", type=Path, required=True)
    pl.add_argument("--insecure", action="store_true", help=argparse.SUPPRESS)

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
    if args.command == "remote":
        return cmd_remote(args, cairn)
    if args.command == "push":
        return cmd_push(args, cairn)
    if args.command == "pull":
        return cmd_pull(args, cairn)
    return 2


def cmd_remote(args: argparse.Namespace, cairn: Cairn) -> int:
    from .client import RemoteError, enroll

    if args.remote_command == "enroll":
        try:
            config = enroll(args.url, args.name, args.secret,
                            platform=sys.platform, insecure=args.insecure)
        except RemoteError as exc:
            print(f"error: enrollment failed: {exc}", file=sys.stderr)
            return 1
        cairn.set_remote(config)
        print(f"enrolled as '{config.device_name}' with {config.url}")
        print("token stored in the local index")
        return 0

    if args.remote_command == "diff":
        return cmd_remote_diff(args, cairn)
    if args.remote_command == "verify":
        return cmd_remote_verify(args, cairn)

    config = cairn.remote_config()
    if config is None:
        print("no remote configured", file=sys.stderr)
        return 1
    print(f"server  {config.url}")
    print(f"device  {config.device_name}")
    try:
        health = cairn.client(insecure=args.insecure).health()
        info = cairn.client(insecure=args.insecure).devices()
    except Exception as exc:
        print(f"unreachable: {exc}", file=sys.stderr)
        return 1
    print(f"health  {health['status']}  ({health['devices']} devices, {health['objects']} objects)")
    print("visible devices:")
    for d in info["visible"]:
        seen = when(d["last_seen_ts"] * 1_000_000_000) if d["last_seen_ts"] else "never"
        print(f"  {d['name']:<20} {d['platform'] or '-':<10} last seen {seen}")
    return 0


def cmd_remote_diff(args: argparse.Namespace, cairn: Cairn) -> int:
    try:
        r = cairn.remote_diff(refresh=args.refresh, insecure=args.insecure)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    source = "asked the server" if r.refreshed else "local cache (--refresh to confirm)"
    print(f"{r.remote}   [{source}]")
    print(f"  {r.backed_up:,} of {r.total:,} readable files backed up  ({r.coverage:.0%})")
    if r.unbackable:
        print(f"  {r.unbackable:,} catalogued but unreadable (placeholders, errors) -- no bytes to send")

    if not r.not_backed_up:
        print("  nothing missing")
        return 0

    print(f"\n  not on the server, highest value first:")
    for path, score, size in r.not_backed_up[: args.limit]:
        print(f"    {score:>5.2f}  {human(size):>9}  {path}")
    if len(r.not_backed_up) > args.limit:
        print(f"    ... and {len(r.not_backed_up) - args.limit:,} more")
    return 0


def cmd_remote_verify(args: argparse.Namespace, cairn: Cairn) -> int:
    try:
        r = cairn.remote_verify(all_objects=args.all, insecure=args.insecure)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if r.checked == 0:
        print("nothing to verify -- this device has pushed no objects")
        return 0

    print(f"server re-hashed {r.checked:,} objects")
    print(f"  {r.ok:,} intact")
    if r.missing:
        print(f"  MISSING {len(r.missing)}:", file=sys.stderr)
        for d in r.missing[:10]:
            print(f"    {d}", file=sys.stderr)
    if r.corrupt:
        print(f"  CORRUPT {len(r.corrupt)}:", file=sys.stderr)
        for d in r.corrupt[:10]:
            print(f"    {d}", file=sys.stderr)
    if r.missing or r.corrupt:
        return 1
    return 0


def cmd_push(args: argparse.Namespace, cairn: Cairn) -> int:
    try:
        r = cairn.push(all=args.all, query=args.query, category=args.category,
                       min_score=args.min_score, limit=args.limit, insecure=args.insecure)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"considered {r.considered:,} files")
    print(f"  {r.uploaded:,} uploaded   {r.already_on_server:,} already on the server")
    print(f"  {r.catalogued:,} catalogue rows pushed")
    print(f"  {human(r.bytes_sent)} sent, {human(r.bytes_skipped)} skipped "
          f"({r.transfer_saved_ratio:.0%} of selected bytes did not move)")
    if r.failed:
        print(f"  {len(r.failed)} failed:", file=sys.stderr)
        for path, why in r.failed[:10]:
            print(f"    {path}: {why}", file=sys.stderr)
        return 1
    return 0


def cmd_pull(args: argparse.Namespace, cairn: Cairn) -> int:
    try:
        out = cairn.pull(sha=args.sha, path=args.path, to=args.to, insecure=args.insecure)
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"pulled and verified {out}")
    return 0


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
    if args.all_devices:
        return cmd_find_remote(args, cairn)

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


def cmd_find_remote(args: argparse.Namespace, cairn: Cairn) -> int:
    try:
        hits = cairn.find_remote(" ".join(args.query), limit=args.limit, insecure=args.insecure)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not hits:
        print("no matches")
        return 1

    width = max(len(h["device"]) for h in hits)
    for i, h in enumerate(hits, 1):
        flag = "" if h["state"] == "indexed" else f" [{h['state']}]"
        print(f"{i:>3}. {h['device']:<{width}}  {h['category'] or '-':<11} {h['score'] or 0:>5.2f}  "
              f"{human(h['size']):>9}  {h['path']}{flag}")
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
