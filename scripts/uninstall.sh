#!/usr/bin/env bash
#
# OpenSpeaksy uninstaller.
#
# Stops and removes the LaunchAgent and logs. Leaves the project directory
# alone — delete it manually if you want.

set -euo pipefail

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LABEL_APP="com.openspeaksy"

echo "==> Unloading LaunchAgent"
launchctl unload "$LAUNCH_AGENTS/${LABEL_APP}.plist" 2>/dev/null || true

echo "==> Removing LaunchAgent plist"
rm -f "$LAUNCH_AGENTS/${LABEL_APP}.plist"

echo "==> Cleaning logs"
rm -f /tmp/openspeaksy.log   # where installs before 2026-11 sent stdout
rm -rf "$HOME/Library/Logs/com.openspeaksy"

cat <<EOF

✓ OpenSpeaksy uninstalled.

The project directory, the Python venv, and the .pending/ queue were left
intact. Remove them manually if you no longer need them:

    rm -rf "$(cd "$(dirname "$0")/.." && pwd)"

Recordings that could not be saved to the project directory were written to a
fallback queue. It holds audio, so remove it too if you are done:

    rm -rf "$HOME/Library/Application Support/OpenSpeaksy"

You may also want to revoke Input Monitoring / Accessibility permissions in
System Settings → Privacy & Security.
EOF
