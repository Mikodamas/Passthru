#!/bin/sh
# Builds Passthru as a Flatpak, installs it for your user, and also writes a
# portable single-file bundle: Passthru.flatpak
#
# Nothing is installed with dnf: flatpak-builder itself runs as a Flatpak if it
# isn't already on your system. Uninstall later with:
#   flatpak uninstall --user io.github.mikodamas.Passthru
set -e
cd "$(dirname "$0")"
APP=io.github.mikodamas.Passthru

flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

if command -v flatpak-builder >/dev/null 2>&1; then
    BUILDER="flatpak-builder"
else
    echo "flatpak-builder not found — using the Flathub version (org.flatpak.Builder)"
    flatpak install --user -y --noninteractive flathub org.flatpak.Builder
    BUILDER="flatpak run org.flatpak.Builder"
fi

# First build downloads the KDE runtime + PyQt BaseApp (~1 GB, shared with other KDE flatpaks).
$BUILDER --user --install-deps-from=flathub --force-clean --install \
    --repo=repo build-dir "$APP.yml"

flatpak build-bundle repo Passthru.flatpak "$APP" \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo

echo
echo "Done!"
echo "  Run it:   flatpak run $APP   (or find Passthru in your app menu)"
echo "  Portable: Passthru.flatpak — install on any machine with: flatpak install --user Passthru.flatpak"
