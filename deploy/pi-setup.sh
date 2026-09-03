#!/usr/bin/env bash
# Install and start the Cairn server on a Raspberry Pi (or any Debian-ish Linux).
#
# Run ON the Pi, from a checkout of the repo:
#     bash deploy/pi-setup.sh
#
# Binds to 127.0.0.1 deliberately. Reach it from the client through an SSH tunnel:
#     ssh -N -L 8823:127.0.0.1:8823 <user>@<pi-address>
#
# Do not change the bind address to 0.0.0.0 until mTLS is in place. The loopback
# bind is currently the only thing standing between the LAN and every document
# the server holds.

set -euo pipefail

CAIRN_HOME="${CAIRN_HOME:-$HOME/cairn}"
CAIRN_DATA="${CAIRN_DATA:-$HOME/cairn-data}"
CAIRN_PORT="${CAIRN_PORT:-8823}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Cairn server setup"
echo "    repo   $REPO_DIR"
echo "    data   $CAIRN_DATA"
echo "    port   $CAIRN_PORT (loopback only)"
echo

# --- sanity ---------------------------------------------------------------

if ! command -v python3 >/dev/null; then
  echo "error: python3 not found. apt install python3 python3-venv" >&2
  exit 1
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
echo "==> python $PYVER on $(uname -m)"
python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit(f"error: Cairn needs Python >= 3.12, found {sys.version.split()[0]}")
PY

# FTS5 is compiled into most SQLite builds but not all; find out now rather than
# at the first search.
python3 - <<'PY'
import sqlite3, sys
c = sqlite3.connect(":memory:")
try:
    c.execute("CREATE VIRTUAL TABLE t USING fts5(b)")
except sqlite3.OperationalError as exc:
    sys.exit(f"error: this SQLite lacks FTS5 ({exc}). Install a python3 built against a fuller SQLite.")
print(f"    sqlite {sqlite3.sqlite_version}, FTS5 ok")
PY

# --- storage --------------------------------------------------------------

mkdir -p "$CAIRN_DATA/store"

DATA_DEV=$(df --output=source "$CAIRN_DATA" | tail -1)
if [[ "$DATA_DEV" == *mmcblk* ]]; then
  echo
  echo "    WARNING: $CAIRN_DATA is on the SD card ($DATA_DEV)."
  echo "    A chunk store does many small writes and will wear it out. Point"
  echo "    CAIRN_DATA at a USB SSD or an NVMe HAT before storing anything real."
  echo
fi

# --- install --------------------------------------------------------------

echo "==> creating venv at $CAIRN_HOME/.venv"
if ! python3 -m venv "$CAIRN_HOME/.venv"; then
  echo "error: could not create a venv. On Raspberry Pi OS / Debian:" >&2
  echo "         sudo apt install -y python3-venv" >&2
  exit 1
fi
"$CAIRN_HOME/.venv/bin/pip" install --quiet --upgrade pip
echo "==> installing cairn[server]"
"$CAIRN_HOME/.venv/bin/pip" install --quiet -e "$REPO_DIR[server]"

# --- systemd --------------------------------------------------------------

UNIT=/etc/systemd/system/cairn-server.service
echo "==> writing $UNIT (needs sudo)"
sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Cairn server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$CAIRN_DATA
ExecStart=$CAIRN_HOME/.venv/bin/cairn-server \\
    --db $CAIRN_DATA/cairn-server.db \\
    --store $CAIRN_DATA/store \\
    --host 127.0.0.1 \\
    --port $CAIRN_PORT
Restart=on-failure
RestartSec=5

# The server only ever needs its own data directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$CAIRN_DATA

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now cairn-server
sleep 2
systemctl --no-pager --lines=0 status cairn-server || true

SECRET=$("$CAIRN_HOME/.venv/bin/cairn-server" --db "$CAIRN_DATA/cairn-server.db" \
         --store "$CAIRN_DATA/store" --show-secret)

cat <<EOF

==> done.

Enrollment secret:
    $SECRET

On the client, open the tunnel:
    ssh -N -L $CAIRN_PORT:127.0.0.1:$CAIRN_PORT $USER@\$(hostname -I | awk '{print \$1}')

then enroll:
    cairn remote enroll --url http://127.0.0.1:$CAIRN_PORT --name my-desktop --secret $SECRET
    cairn scan C:\\Users\\you\\Documents
    cairn push --min-score 0.6

Logs:  journalctl -u cairn-server -f
EOF
          