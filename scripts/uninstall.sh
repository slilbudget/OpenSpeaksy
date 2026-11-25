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

echo "==> Revoking its permissions"
# The grants belong to OpenSpeaksy.app's bundle id, so this clears exactly
# them (installs before 1.5 ran as "Python": remove that entry by hand).
tccutil reset All com.openspeaksy >/dev/null 2>&1 || true

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

Its permissions were revoked. If System Settings → Privacy & Security still
lists "Python" from an older install, remove that entry with the − button.
EOF
