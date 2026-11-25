#!/usr/bin/env bash
#
# OpenSpeaksy installer for macOS.
#
# Sets up the Python venv, builds OpenSpeaksy.app (the launcher macOS asks
# permissions for) and the LaunchAgent that runs main.py through it.
# Requires a Mistral API key and writes it into the plist's
# EnvironmentVariables. Mistral handles both speech-to-text and translation.
#
# Usage:   ./scripts/install.sh
# Env:     PYTHON_RUNTIME=python3.13
#          MISTRAL_API_KEY=key      (skip the Mistral prompt)

set -euo pipefail

# --- config -----------------------------------------------------------------

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LABEL_APP="com.openspeaksy"

PYTHON_RUNTIME="${PYTHON_RUNTIME:-python3.13}"

cd "$PROJECT_ROOT"

# --- helpers ----------------------------------------------------------------

step()  { printf "\n\033[1;36m==>\033[0m %s\n" "$1"; }
note()  { printf "    %s\n" "$1"; }
fail()  { printf "\n\033[1;31m✗\033[0m %s\n" "$1" >&2; exit 1; }

# --- preflight --------------------------------------------------------------

step "Checking platform"
[[ "$(uname -s)" == "Darwin" ]] || fail "macOS only"

step "Checking Homebrew"
if ! command -v brew &>/dev/null; then
    note "Installing Homebrew"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
fi

step "Ensuring Python interpreter"
command -v "$PYTHON_RUNTIME" &>/dev/null || brew install "${PYTHON_RUNTIME/python/python@}"
# A freshly installed keg may not be on PATH yet (new shell hasn't sourced
# brew shellenv) — fall back to the absolute keg path.
if ! command -v "$PYTHON_RUNTIME" &>/dev/null; then
    PYTHON_RUNTIME="$(brew --prefix)/bin/$PYTHON_RUNTIME"
    command -v "$PYTHON_RUNTIME" &>/dev/null || fail "Python interpreter not found after install"
fi

# --- API keys ---------------------------------------------------------------

step "Configuring Mistral API key"
if [[ -z "${MISTRAL_API_KEY:-}" ]]; then
    cat <<EOF
    OpenSpeaksy uses Mistral for speech-to-text and translation.
    Create an API key at: https://console.mistral.ai/api-keys

    The key is written only into your local plist
    ($LAUNCH_AGENTS/${LABEL_APP}.plist) — never to this repo.

EOF
    read -rs -p "    Paste your Mistral API key: " MISTRAL_API_KEY
    echo
fi
[[ -n "$MISTRAL_API_KEY" ]] || fail "no Mistral API key provided"
note "Got the Mistral key"

# --- main app venv ----------------------------------------------------------

step "Creating Python venv for the app"
# --clear rebuilds a stale venv left by a previous install (e.g. after a
# Homebrew Python upgrade broke the interpreter symlinks).
"$PYTHON_RUNTIME" -m venv --clear venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
deactivate

# --- app --------------------------------------------------------------------

step "Building OpenSpeaksy.app"
# Kept as is when its sources did not change. A new build is a new identity
# to macOS: switches turned on for the old one no longer apply, though they
# still show as on. Clear them, so the app asks again and the user sees
# exactly what to turn on.
had_app=0
[[ -d "$PROJECT_ROOT/OpenSpeaksy.app" ]] && had_app=1
built="$("$PROJECT_ROOT/scripts/build_app.sh")" || fail "could not build OpenSpeaksy.app (see above)"
if [[ "$built" == built* && $had_app == 1 ]]; then
    tccutil reset All com.openspeaksy >/dev/null 2>&1 || true
    note "The app changed: macOS will ask for its permissions again"
fi

# --- LaunchAgent ------------------------------------------------------------

step "Generating LaunchAgent plist"
mkdir -p "$LAUNCH_AGENTS"
# launchd opens the stdout/stderr log before the app's umask applies, so the
# folder itself has to keep it private: a traceback can carry error text.
mkdir -p "$HOME/Library/Logs/${LABEL_APP}"
chmod 700 "$HOME/Library/Logs/${LABEL_APP}"

# Use Python's plistlib so paths and key values with XML-sensitive characters
# are escaped correctly — sed-substitution would corrupt the plist.
MISTRAL_API_KEY="$MISTRAL_API_KEY" \
"$PYTHON_RUNTIME" - "$PROJECT_ROOT/launchd/${LABEL_APP}.plist.template" \
                    "$LAUNCH_AGENTS/${LABEL_APP}.plist" \
                    "$PROJECT_ROOT" "$HOME" <<'PYEOF'
import os, sys, plistlib
template, target, project_root, home = sys.argv[1:5]
mistral_key = os.environ.pop("MISTRAL_API_KEY")
with open(template, "rb") as f:
    pl = plistlib.load(f)

def replace(node):
    if isinstance(node, list):
        return [replace(x) for x in node]
    if isinstance(node, dict):
        return {k: replace(v) for k, v in node.items()}
    if isinstance(node, str):
        return (node.replace("__PROJECT_ROOT__", project_root)
                    .replace("__HOME__", home)
                    .replace("__MISTRAL_API_KEY__", mistral_key))
    return node

# Open with 0600 from the start so the API key is never world-readable,
# even briefly. os.open + plistlib.dump on the resulting fd avoids the
# default-umask window an open(target, "wb") + os.chmod sequence leaves.
# Written to a temp file and renamed into place, so a crash never leaves a
# cut-off plist, and fchmod'ed before the key goes in: os.open's mode only
# applies on creation, and a leftover temp file could be looser.
tmp = target + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.fchmod(fd, 0o600)
with os.fdopen(fd, "wb") as f:
    plistlib.dump(replace(pl), f)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, target)
PYEOF

step "Loading LaunchAgent"
APP_LOG="$HOME/Library/Logs/${LABEL_APP}/main.log"
# Where this start's lines begin: the wait below must not take an earlier
# run's lines for this one.
log_before="$( (wc -l < "$APP_LOG") 2>/dev/null || echo 0)"
launchctl unload "$LAUNCH_AGENTS/${LABEL_APP}.plist" 2>/dev/null || true
launchctl load   "$LAUNCH_AGENTS/${LABEL_APP}.plist"

# --- finish -----------------------------------------------------------------

printf "\n\033[1;32m✓ OpenSpeaksy installed.\033[0m\n\n"
cat <<EOF
Next: macOS asks three things, one after the other.

  1. The microphone: click Allow.
  2. "OpenSpeaksy would like to receive keystrokes": click
     Open System Settings, turn ON the switch next to OpenSpeaksy.
  3. "OpenSpeaksy would like to control this computer": the same,
     Open System Settings, turn ON OpenSpeaksy.
     (On the newest macOS that pane is called Device Control and Data Access.)

After each switch the app restarts by itself and asks the next one, if any.
(If the microphone was allowed before, that first one is skipped.)
To check:

  "$PROJECT_ROOT/scripts/doctor.sh"

Using it

  Hold right Command or right Option   dictate
  Both together                        dictate hands-free; tap either to stop
  Hold right Shift                     speak Russian, paste English
  Caps Lock                            cancel a stuck transcription

  Words it mishears: cp "$PROJECT_ROOT/vocabulary.example.txt" "$PROJECT_ROOT/vocabulary.txt"
                     and list them there

  Logs:    tail -f ~/Library/Logs/com.openspeaksy/main.log
  Stop:    launchctl unload ~/Library/LaunchAgents/com.openspeaksy.plist
  Remove:  "$PROJECT_ROOT/scripts/uninstall.sh"

To rotate an API key later, edit
$LAUNCH_AGENTS/${LABEL_APP}.plist
and re-run: launchctl unload ... && launchctl load ...
EOF

# Wait (up to 20 s) for the app to start and log what macOS granted, then
# show where things stand. No --open here: the app's own prompt offers to
# open System Settings, and a second window would only compete with it.
for _ in $(seq 1 20); do
    # || true: no log yet (or no match) is the normal first answer, and
    # under set -e with pipefail it would end the script right here.
    started="$(grep -n "OpenSpeaksy starting" "$APP_LOG" 2>/dev/null | tail -1 | cut -d: -f1 || true)"
    reported="$(grep -n "post events trusted:" "$APP_LOG" 2>/dev/null | tail -1 | cut -d: -f1 || true)"
    [[ -n "$started" && -n "$reported" && "$started" -gt "$log_before" &&
       "$reported" -gt "$started" ]] && break
    sleep 1
done
echo
"$PROJECT_ROOT/scripts/doctor.sh" || true
