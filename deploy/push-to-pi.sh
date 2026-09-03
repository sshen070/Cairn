#!/usr/bin/env bash
# Copy this working tree to the server and run the setup there.
#
# Run from the repo root on the client (git-bash on Windows is fine):
#     bash deploy/push-to-pi.sh              # defaults to pi@192.168.0.222
#     bash deploy/push-to-pi.sh jay          # different user, default host
#     bash deploy/push-to-pi.sh jay@10.0.0.5 # both
#
# Works without key authentication. The script opens ONE ssh connection, lets
# you type the password once, and then multiplexes every later ssh and tar over
# that same connection through a control socket -- so you are not prompted three
# separate times. If a key does happen to work, it is used and nothing prompts.
#
# Uses tar over ssh rather than rsync, because git-bash ships tar and ssh but
# not rsync. Only the source is copied -- the venv, the git history, generated
# fixtures, and any local store stay on this machine.

set -euo pipefail

DEFAULT_HOST="192.168.0.222"
DEFAULT_USER="pi"
REMOTE_DIR="${REMOTE_DIR:-cairn}"
PORT="${CAIRN_PORT:-8823}"

# --- work out who and where ------------------------------------------------

ARG="${1:-}"
case "$ARG" in
  -h|--help)
    sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  -*)
    echo "error: unknown option '$ARG'. Expected [user][@host], or --help." >&2
    exit 2
    ;;
esac

if [[ -z "$ARG" ]]; then
  TARGET="${DEFAULT_USER}@${DEFAULT_HOST}"
elif [[ "$ARG" == *@* ]]; then
  TARGET="$ARG"
else
  TARGET="${ARG}@${DEFAULT_HOST}"           # bare word means "user"
fi

USER_PART="${TARGET%@*}"
HOST_PART="${TARGET#*@}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# --- connection strategy ---------------------------------------------------

# Connection multiplexing would let you authenticate once and reuse the socket
# for every later command. It does NOT work in the git-bash / MSYS2 OpenSSH
# build: the master authenticates, then the mux client cannot attach to the
# control socket --
#
#     mux_client_request_session: read from master failed: Connection reset by peer
#     Failed to connect to new control master
#
# (observed with OpenSSH 10.0p2 under MINGW64). Plain ssh is completely fine;
# only the multiplexing layer is broken. So on Windows we connect twice and say
# so up front, rather than failing after the password has already been typed.
USE_MUX=1
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) USE_MUX=0 ;;
esac

SSH_OPTS=()
CTL=""
cleanup() { :; }

if [[ "$USE_MUX" == "1" ]]; then
  CTL="/tmp/cairn-ssh-${USER_PART}-${HOST_PART//[^a-zA-Z0-9]/_}-$$"
  SSH_OPTS=(-o "ControlMaster=auto" -o "ControlPath=$CTL" -o "ControlPersist=600")
  cleanup() {
    ssh -O exit -o "ControlPath=$CTL" "$TARGET" 2>/dev/null || true
    rm -f "$CTL" 2>/dev/null || true
  }
fi
trap cleanup EXIT

sshx() { ssh "${SSH_OPTS[@]}" "$@"; }

echo "==> target $TARGET"

# Try key auth once, quietly. If it works, nothing below will prompt at all.
if ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" 'true' 2>/dev/null; then
  echo "    authenticated with an ssh key"
  HAVE_KEY=1
else
  HAVE_KEY=0
  # PubkeyAuthentication=no goes straight to the password prompt instead of
  # offering every key first, which otherwise risks "too many authentication
  # failures" before a password is ever requested.
  SSH_OPTS+=(-o "PubkeyAuthentication=no")
  PROMPTS=$([[ "$USE_MUX" == "1" ]] && echo "once" || echo "twice")
  echo
  echo "    No usable ssh key, so you will be asked for ${USER_PART}'s password"
  echo "    on ${HOST_PART} ${PROMPTS} -- first to copy the source, then to run"
  echo "    the setup. To stop being asked at all:"
  echo "        ssh-copy-id -i ~/.ssh/id_rsa.pub ${TARGET}"
  echo
fi

# --- copy the source -------------------------------------------------------

echo "==> copying source to $TARGET:~/$REMOTE_DIR"
tar czf - \
  --exclude='./.venv' \
  --exclude='./.git' \
  --exclude='./fixtures' \
  --exclude='./store' \
  --exclude='./demo' \
  --exclude='./restored' \
  --exclude='./.pytest_cache' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*.egg-info' \
  . | sshx "$TARGET" "
      case \"\$(uname -s)\" in
        Linux*) ;;
        *) echo '    warning: remote is not Linux; pi-setup.sh assumes systemd' >&2 ;;
      esac
      mkdir -p ~/$REMOTE_DIR && tar xzf - -C ~/$REMOTE_DIR && echo \"    unpacked to ~/$REMOTE_DIR on \$(uname -m)\""

# --- run the setup ---------------------------------------------------------

# -t so sudo inside pi-setup.sh can prompt for its own password on a real tty.
echo "==> running setup on $TARGET"
sshx -t "$TARGET" "cd ~/$REMOTE_DIR && bash deploy/pi-setup.sh"

cat <<EOF

==> next, from this machine:

    # leave this running in its own terminal (it will ask for the password again)
    ssh -N -L ${PORT}:127.0.0.1:${PORT} ${TARGET}

    # then, in another terminal
    cairn remote enroll --url http://127.0.0.1:${PORT} --name win-desktop --secret <secret above>
    cairn scan "\$USERPROFILE/Downloads"
    cairn push --min-score 0.6
    cairn find tax --all-devices

Getting rid of the password prompts entirely:

    ssh-copy-id -i ~/.ssh/id_rsa.pub ${TARGET}

EOF
