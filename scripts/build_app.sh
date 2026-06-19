#!/usr/bin/env bash
# Build a double-clickable Mimic.app that launches the live viewer (mimic/ios/viewer.py).
# Usage:  scripts/build_app.sh [/path/to/Mimic.app]   (default: ~/Desktop/Mimic.app)
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="${1:-$HOME/Desktop/Mimic.app}"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$REPO/assets/Mimic.icns" "$APP/Contents/Resources/Mimic.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Mimic</string>
  <key>CFBundleDisplayName</key><string>Mimic</string>
  <key>CFBundleIdentifier</key><string>com.botnick.mimic.viewer</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>Mimic</string>
  <key>CFBundleIconFile</key><string>Mimic</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/Mimic" <<SH
#!/bin/bash
export PYTHONPATH="$REPO:\$PYTHONPATH"
exec /usr/bin/python3 -m mimic.ios.viewer
SH
chmod +x "$APP/Contents/MacOS/Mimic"
touch "$APP"
echo "Built $APP"
