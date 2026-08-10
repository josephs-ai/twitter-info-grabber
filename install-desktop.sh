#!/usr/bin/env bash
# Add the app to your desktop launcher (Linux, freedesktop.org).
# Reverse with: rm ~/.local/share/applications/ai-signal-tracker.desktop
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
DEST="$HOME/.local/share/applications"
mkdir -p "$DEST"

cat > "$DEST/ai-signal-tracker.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=AI Signal Tracker
Comment=What is actually new in AI today
Exec=$ROOT/app
Icon=$ROOT/ui/assets/icon.svg
Terminal=false
Categories=Network;News;
Keywords=ai;research;digest;twitter;
StartupWMClass=tracker
DESKTOP

chmod +x "$DEST/ai-signal-tracker.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DEST" 2>/dev/null || true
echo "Installed: $DEST/ai-signal-tracker.desktop"
echo "Look for 'AI Signal Tracker' in your app grid."
