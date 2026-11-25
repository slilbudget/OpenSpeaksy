#!/usr/bin/env bash
#
# Builds OpenSpeaksy.app in the project root: a small launcher that runs the
# Python app as its child, so macOS asks for and lists permissions under
# "OpenSpeaksy" (see app/launcher.c).
#
# macOS ties a grant to this exact build (ad-hoc signature = its code hash),
# so an app built from the same sources is kept, not rebuilt: rebuilding with
# a newer compiler would turn the switches the user flipped off again. The
# sources travel inside the bundle (sealed by the signature) to compare with.
#
# Usage: ./scripts/build_app.sh     prints the app's path
# Needs: clang (Xcode Command Line Tools; Homebrew installs them)

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$PROJECT_ROOT/app"
APP="$PROJECT_ROOT/OpenSpeaksy.app"

up_to_date() {
    [[ -x "$APP/Contents/MacOS/OpenSpeaksy" ]] &&
    cmp -s "$SRC/launcher.c" "$APP/Contents/Resources/launcher.c" &&
    cmp -s "$SRC/Info.plist" "$APP/Contents/Info.plist" &&
    cmp -s "$SRC/OpenSpeaksy.icns" "$APP/Contents/Resources/OpenSpeaksy.icns" &&
    codesign --verify "$APP" 2>/dev/null
}

if up_to_date; then
    echo "$APP"
    exit 0
fi

command -v clang &>/dev/null || {
    echo "clang not found: run xcode-select --install, then this again" >&2
    exit 1
}

STAGE="$(mktemp -d "$PROJECT_ROOT/.OpenSpeaksy.app.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
BUILD="$STAGE/OpenSpeaksy.app"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
cp "$SRC/Info.plist" "$BUILD/Contents/Info.plist"
cp "$SRC/launcher.c" "$SRC/OpenSpeaksy.icns" "$BUILD/Contents/Resources/"
clang -Os -Wall -o "$BUILD/Contents/MacOS/OpenSpeaksy" "$SRC/launcher.c"
codesign --sign - --force --identifier com.openspeaksy "$BUILD" >/dev/null 2>&1
codesign --verify "$BUILD"

rm -rf "$APP"
mv "$BUILD" "$APP"
echo "$APP"
