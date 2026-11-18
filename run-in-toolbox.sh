#!/bin/sh
# Run Passthru inside a Fedora toolbox container, so nothing gets installed on your main system.
# First run: creates the container and installs dependencies into it (takes a minute).
# Later runs: starts instantly.
# To remove everything again:  toolbox rm -f passthru
set -e
NAME=passthru
DIR="$(cd "$(dirname "$0")" && pwd)"

if ! toolbox list --containers | grep -qw "$NAME"; then
    echo "Creating toolbox container '$NAME'…"
    toolbox create -y "$NAME"
    toolbox run -c "$NAME" sudo dnf install -y python3-pyqt6 python3-gobject \
        gstreamer1-plugins-base gstreamer1-plugins-good pipewire-gstreamer
fi

exec toolbox run -c "$NAME" python3 "$DIR/passthru.py" "$@"
