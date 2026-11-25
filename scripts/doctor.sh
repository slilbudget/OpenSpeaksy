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
else
    bad "Python venv missing"
    fix "run ./scripts/install.sh"
fi

APP="$PROJECT_ROOT/OpenSpeaksy.app"
if [[ -x "$APP/Contents/MacOS/OpenSpeaksy" ]] && codesign --verify "$APP" 2>/dev/null; then
    ok "OpenSpeaksy.app"
else
    bad "OpenSpeaksy.app missing or broken"
    fix "run ./scripts/install.sh"
fi

if [[ -f "$PLIST" ]]; then
    ok "LaunchAgent installed"
    runs="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$PLIST" 2>/dev/null || true)"
    if [[ "$runs" != "$APP/Contents/MacOS/OpenSpeaksy" ]]; then
        if [[ "$runs" == */OpenSpeaksy.app/Contents/MacOS/OpenSpeaksy ]]; then
            bad "the LaunchAgent starts an OpenSpeaksy.app elsewhere: $runs"
            fix "the project moved; run ./scripts/install.sh from here"
        else
            bad "the LaunchAgent starts Python directly (an install from before 1.5)"
            fix "run ./scripts/install.sh again: permissions then show as OpenSpeaksy"
        fi
    fi
    unset runs
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

row="$(launchctl list 2>/dev/null | awk -v l="$LABEL" '$3 == l {print $1}')"
loaded="${row:+yes}"
pid="$row"
if [[ -n "$pid" && "$pid" != "-" ]]; then
    ok "app running (pid $pid)"
elif [[ -n "$loaded" ]]; then
    # Loaded but between runs. A restart after a permission grant passes in
    # seconds; if it stays like this, the app keeps dying before it logs, and
    # the log below still shows an older, healthy start.
    bad "app not running right now (launchd restarts it)"
    fix "wait ~10 s and run this again; if it stays, see ~/Library/Logs/com.openspeaksy/stdout.log"
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
    # "event tap active" alone proves nothing: without Input Monitoring
    # macOS hands out the tap anyway, blind to every key.
    if grep -q "input monitoring trusted: True" <<<"$block" &&
       grep -q "event tap active" <<<"$block"; then
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
    fix "System Settings → Privacy & Security → Microphone → turn on OpenSpeaksy"
    [[ $OPEN_PANES == 1 ]] && open "$PANE_MIC"
else
    bad "Microphone not answered yet"
    fix "click Allow in the macOS dialog (or turn on OpenSpeaksy in Microphone)"
fi

if (( need_input || need_access )); then
    echo
    echo "  To fix: System Settings → Privacy & Security →"
    (( need_input ))  && echo "    • Input Monitoring"
    (( need_access )) && echo "    • Accessibility (on the newest macOS: Device Control and Data Access)"
    echo "    → turn ON the switch next to OpenSpeaksy."
    echo "  The app asks for one at a time and is already in that list: it only"
    echo "  needs switching on. It restarts by itself within seconds and then"
    echo "  asks for the next one."
    echo "  If OpenSpeaksy is not listed, click + and pick (⇧⌘G pastes a path):"
    echo "    $APP"
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
