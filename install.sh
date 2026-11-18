#!/bin/sh
# Installs Passthru for the current user (no root needed for this part).
set -e
cd "$(dirname "$0")"

echo "Installing dependencies (needs sudo)…"
sudo dnf install -y python3-pyqt6 python3-gobject gstreamer1-plugins-base \
    gstreamer1-plugins-good pipewire-gstreamer

install -Dm755 passthru.py "$HOME/.local/bin/passthru"
install -Dm644 passthru.desktop "$HOME/.local/share/applications/passthru.desktop"
install -Dm644 io.github.mikodamas.Passthru.svg "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.mikodamas.Passthru.svg"
sed -i "s|^Exec=.*|Exec=$HOME/.local/bin/passthru|" "$HOME/.local/share/applications/passthru.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "Done. Launch Passthru from the app menu, or run: passthru"
