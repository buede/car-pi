#!/usr/bin/env bash
#
# Install car-pi as a system service on a Raspberry Pi.
#
# NOT YET VERIFIED ON HARDWARE. Run with --dry-run first.
#
# Installs into a virtualenv at /opt/carpi/venv, owned by an unprivileged `carpi`
# user. Nothing car-pi does needs root: once the CAN interface is up, reading it
# requires no privilege at all.

set -euo pipefail

DRY_RUN=0
PREFIX="/opt/carpi"
SERVICE_USER="carpi"
SOURCE=""

usage() {
    sed -n '3,11p' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  --dry-run          print what would happen, change nothing
  --prefix PATH      install location (default: $PREFIX)
  --user NAME        service account (default: $SERVICE_USER)
  --source PATH      install from a working tree instead of the repository root
  -h, --help         this message
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --prefix) PREFIX="$2"; shift ;;
        --user) SERVICE_USER="$2"; shift ;;
        --source) SOURCE="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "setup-service.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

HERE="$(cd "$(dirname "$0")" && pwd)"
[ -n "$SOURCE" ] || SOURCE="$(dirname "$HERE")"

say() { echo "setup-service.sh: $*" >&2; }

do_or_show() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  would run: $*" >&2
    else
        "$@"
    fi
}

if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    say "must run as root (or pass --dry-run)"
    exit 1
fi

if [ ! -f "$SOURCE/pyproject.toml" ]; then
    say "no pyproject.toml under $SOURCE; pass --source"
    exit 1
fi

say "source:  $SOURCE"
say "prefix:  $PREFIX"
say "user:    $SERVICE_USER"

if id "$SERVICE_USER" >/dev/null 2>&1; then
    say "user $SERVICE_USER already exists"
else
    # A system account: no login shell, no home directory worth having.
    do_or_show useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

do_or_show install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$PREFIX"

if [ -x "$PREFIX/venv/bin/python" ]; then
    say "virtualenv already present, reusing it"
else
    do_or_show python3 -m venv "$PREFIX/venv"
fi

# --upgrade so re-running this picks up a newer working tree.
do_or_show "$PREFIX/venv/bin/pip" install --upgrade pip
do_or_show "$PREFIX/venv/bin/pip" install --upgrade "$SOURCE"
do_or_show chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"

# Verify the definition database survived packaging before enabling anything. A
# missing defs directory produces a service that starts and then fails every scan,
# which is a far more confusing failure than this check.
if [ "$DRY_RUN" -eq 0 ]; then
    if ! "$PREFIX/venv/bin/carpi" defs check >/dev/null; then
        say "the installed package cannot load its definition database; not enabling"
        exit 1
    fi
    say "definition database loads correctly"
fi

do_or_show install -m 0644 "$HERE/carpi.service" /etc/systemd/system/carpi.service
do_or_show systemctl daemon-reload
do_or_show systemctl enable carpi.service

cat >&2 <<EOF

setup-service.sh: done.

    sudo systemctl start carpi
    systemctl status carpi
    journalctl -u carpi -f

The UI will be on port 8080. Over the hotspot that is http://10.42.0.1:8080/ once
setup-hotspot.sh has run; before that, use the Pi's address on your existing network.
EOF
