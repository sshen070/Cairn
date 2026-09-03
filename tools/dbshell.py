"""Interactive poking at a Cairn index.  Usage: python -i tools/dbshell.py [db]

Startup file, not a module -- `python -i` leaves you at a prompt with the
helpers below already bound. Read-only queries only; nothing here writes.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from cairn.db import fts_quote  # noqa: E402

DB = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CAIRN_DB", "cairn.db")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row


def q(sql, *params):
    """Run `sql` and print each row as a dict. Pass values as ? params."""
    rows = conn.execute(sql, params).fetchall()
    for r in rows:
        print(dict(r))
    print(f"({len(rows)} rows)")


def tables():
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type=? ORDER BY name", ("table",)):
        print(r[0])


def schema(table):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
    print(row[0] if row else f"no such table: {table}")


def search(text, limit=10):
    """Full-text search, with the user's string quoted the way cairn does it."""
    q(
        "SELECT f.name, f.category, f.score FROM search s"
        " JOIN files f ON f.id = s.rowid WHERE search MATCH ?"
        " ORDER BY f.score DESC LIMIT ?",
        fts_quote(text),
        limit,
    )


print(f"cairn db: {DB}")
print("helpers: q(sql, *params)  tables()  schema('files')  search('tax return')")
