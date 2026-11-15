#!/usr/bin/env bash
# Grab-and-run: fetch SlickStick to a temporary directory, launch the wizard,
# and clean up on exit. Nothing is installed; nothing is left behind.
#
#   curl -fsSL https://raw.githubusercontent.com/mjevange/slickstick/main/run.sh | bash
#
# Pass arguments with `-s --`, e.g.:
#   curl -fsSL https://raw.githubusercontent.com/mjevange/slickstick/main/run.sh | bash -s -- devices
#
# Works in any shell (bash, zsh, fish). This script reads /dev/tty directly, so
# the wizard's prompts work even though the bootstrap arrived over the pipe.
set -euo pipefail

REPO="https://github.com/mjevange/slickstick"

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: required tool '$1' not found"; exit 1; }; }
need git
need python3

TMP="$(mktemp -d "${TMPDIR:-/tmp}/slickstick.XXXXXX")"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo "Fetching SlickStick (temporary — nothing installed)…"
git clone --depth 1 --quiet "$REPO" "$TMP/slickstick"

# Run (not exec) so the cleanup trap still fires afterward. Preserve exit code.
# Read straight from the terminal so the wizard's prompts work even if this
# bootstrap arrived over a pipe (curl ... | bash), where stdin is the script.
set +e
if { : < /dev/tty; } 2>/dev/null; then
  python3 "$TMP/slickstick/slickstick.py" "$@" < /dev/tty
else
  python3 "$TMP/slickstick/slickstick.py" "$@"
fi
rc=$?
set -e
exit "$rc"
