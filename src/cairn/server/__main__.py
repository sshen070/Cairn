"""Run the Cairn server.

    python -m cairn.server --db cairn-server.db --store /srv/cairn/store

Binds to 127.0.0.1 by default, on purpose. Behind an SSH tunnel that loopback
bind *is* the access control: the only way to reach the socket is to already be
authenticated to the host. Pass --host 0.0.0.0 only once mTLS is in place --
until then it exposes every catalogued document to anything on the LAN.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .app import create_app
from .schema import connect, enrollment_secret


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cairn-server", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=Path("cairn-server.db"))
    p.add_argument("--store", type=Path, default=Path("cairn-store"))
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1 -- see the warning above)")
    p.add_argument("--port", type=int, default=8823)
    p.add_argument("--show-secret", action="store_true", help="print the enrollment secret and exit")
    args = p.parse_args(argv)

    conn = connect(args.db)
    secret = enrollment_secret(conn)

    if args.show_secret:
        print(secret)
        return 0

    print(f"cairn server")
    print(f"  index  {args.db.resolve()}")
    print(f"  store  {args.store.resolve()}")
    print(f"  bind   {args.host}:{args.port}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING: bound beyond loopback with no transport auth configured.")
    print(f"  enrollment secret: {secret}")
    print()

    import uvicorn

    uvicorn.run(create_app(args.db, args.store), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
