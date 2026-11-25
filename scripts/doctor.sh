#!/usr/bin/env bash
#
# OpenSpeaksy doctor: is the install complete, and if not, what is missing.
#
# Reads what the running app itself logged at its last start, because macOS
# grants permissions per binary: this shell's own permissions say nothing
# about the Python that launchd runs.
#
# Usage:   ./scripts/doctor.sh          check and explain
#          ./scripts/doctor.sh --open   also open the System Settings panes
#                                       for whatever is still missing
# Exit:    0 when everything works, 1 otherwise.

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.openspeaksy"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/${LABEL}/main.log"
OPEN_PANES=0
[[ "${1:-}" == "--open" ]] && OPEN_PANES=1

PANE_INPUT="x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
PANE_ACCESS="x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
PANE_MIC="x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"

problems=0
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; problems=$((problems + 1)); }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fix()  { printf "      → %s\n" "$1"; }

echo "OpenSpeaksy doctor"
echo

# --- install ----------------------------------------------------------------

if [[ -x "$PROJECT_ROOT/venv/bin/python" ]]; then
    ok "Python venv"
    REAL_PYTHON="$("$PROJECT_ROOT/venv/bin/python" -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null)"
else
    bad "Python venv missing"
    fix "run ./scripts/install.sh"
    REAL_PYTHON=""
fi

if [[ -f "$PLIST" ]]; then
    ok "LaunchAgent installed"
    key="$(/usr/libexec/PlistBuddy -c "Print :EnvironmentVariables:MISTRAL_API_KEY" "$PLIST" 2>/dev/null || true)"
    if [[ -n "$key" && "$key" != "__MISTRAL_API_KEY__" ]]; then
        ok "Mistral API key set"
    else
        bad "Mistral API key missing"
        fix "re-run ./scripts/install.sh with the key"
    fi
    unset key
else
    bad "LaunchAgent not installed"
    fix "run ./scripts/install.sh"
fi

loaded="$(launchctl list 2>/dev/null | awk -v l="$LABEL" '$3 == l {print "yes"}')"
pid="$(launchctl list 2>/dev/null | awk -v l="$LABEL" '$3 == l {print $1}')"
if [[ -n "$pid" && "$pid" != "-" ]]; then
    ok "app running (pid $pid)"
elif [[ -n "$loaded" ]]; then
    # Loaded but between runs: launchd relaunches it every few seconds, which
    # is what a missing Input Monitoring grant looks like (see below).
    warn "app loaded but restarting (launchd retries every few seconds)"
else
    bad "app not running"
    fix "launchctl load $PLIST  (then wait a few seconds and run this again)"
fi

# --- what the app saw at its last start ---------------------------------------

if [[ ! -f "$LOG" ]]; then
    bad "no log yet at $LOG"
    fix "the app has not started; see above"
    echo
    exit 1
fi

start_line="$(grep -n "OpenSpeaksy starting" "$LOG" | tail -1 | cut -d: -f1)"
block="$(tail -n +"${start_line:-1}" "$LOG")"

if grep -q "FATAL" <<<"$block"; then
    bad "$(grep "FATAL" <<<"$block" | tail -1 | cut -c21-)"
fi

need_input=0
need_access=0
if ! grep -q "post events trusted:" <<<"$block"; then
    bad "the app has not reported its permissions yet (still starting?)"
    fix "wait ~10 s and run this again"
else
    if grep -q "event tap active" <<<"$block"; then
        # The tap is what the hotkeys need; Accessibility alone can be
        # enough for it, so this, not the Input Monitoring flag, decides.
        ok "hotkeys listening (Input Monitoring)"
    else
        bad "hotkeys off: Input Monitoring is not on"
        need_input=1
    fi
    if grep -q "post events trusted: True" <<<"$block"; then
        ok "pasting with ⌘V (Accessibility)"
    else
        bad "Accessibility is off: text is only copied, not pasted"
        need_access=1
    fi
fi

if grep -q "microphone permission trusted: True" <<<"$block"; then
    ok "Microphone"
elif grep -q "Microphone permission is denied" <<<"$block"; then
    bad "Microphone is blocked"
    fix "System Settings → Privacy & Security → Microphone → turn on Python"
    [[ $OPEN_PANES == 1 ]] && open "$PANE_MIC"
else
    warn "Microphone not asked yet: on the first dictation macOS asks, click Allow"
fi

if (( need_input || need_access )); then
    echo
    echo "  To fix: System Settings → Privacy & Security →"
    (( need_input ))  && echo "    • Input Monitoring → turn ON the entry named Python (or python3.x; if two, both)"
    (( need_access )) && echo "    • Accessibility (on the newest macOS: Device Control and Data Access)"
    (( need_access )) && echo "      → turn ON the entry named Python (or python3.x; if two, both)"
    echo "  The app has already asked macOS, so Python is in those lists; it only"
    echo "  needs switching on."
    (( need_access )) && echo "  Once Accessibility is on, the app restarts by itself within seconds."
    if [[ -n "$REAL_PYTHON" ]]; then
        echo "  If Python is not listed, click + and pick this file (⇧⌘G pastes a path):"
        echo "    $REAL_PYTHON"
    fi
    if [[ $OPEN_PANES == 1 ]]; then
        # One pane at a time: a second open replaces the first. Run again
        # after this one is done for the next.
        if (( need_input )); then open "$PANE_INPUT"; else open "$PANE_ACCESS"; fi
    else
        echo "  ./scripts/doctor.sh --open opens the pane to fix."
    fi
fi

echo
if (( problems == 0 )); then
    printf "\033[32mAll set.\033[0m Hold right ⌘ (or right ⌥), speak, release: the text appears at the cursor.\n"
    exit 0
fi
echo "$problems problem(s). Fix them, wait ~10 s, and run ./scripts/doctor.sh again."
exit 1
