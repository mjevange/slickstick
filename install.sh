#!/usr/bin/env bash
# SlickStick bootstrap installer.
#
# Install the `slickstick` command:
#   bash <(curl -fsSL https://raw.githubusercontent.com/mjevange/slickstick/main/install.sh)
#
# Install and launch the wizard immediately:
#   bash <(curl -fsSL https://raw.githubusercontent.com/mjevange/slickstick/main/install.sh) --run
#
# Use `bash <(curl ...)` (process substitution) rather than `curl ... | bash` so
# the wizard's prompts can still read your keyboard.
set -euo pipefail

REPO="https://github.com/mjevange/slickstick"
DEST="${SLICKSTICK_HOME:-$HOME/.local/share/slickstick}"
BIN="$HOME/.local/bin"
LAUNCHER="$BIN/slickstick"

need() { command -v "$1" >/dev/null 2>&1 || { echo "error: required tool '$1' not found"; exit 1; }; }
need git
need python3

echo "Installing SlickStick -> $DEST"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" pull --ff-only --quiet || {
    echo "  existing copy has diverged; re-cloning"
    rm -rf "$DEST"; git clone --depth 1 --quiet "$REPO" "$DEST"
  }
else
  mkdir -p "$(dirname "$DEST")"
  git clone --depth 1 --quiet "$REPO" "$DEST"
fi

mkdir -p "$BIN"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec python3 "$DEST/slickstick.py" "\$@"
EOF
chmod +x "$LAUNCHER"
echo "Launcher installed -> $LAUNCHER"

# Show dependency status so the user knows what (if anything) is still needed.
echo
python3 "$DEST/slickstick.py" deps || true

if ! case ":$PATH:" in *":$BIN:"*) true;; *) false;; esac; then
  echo
  echo "note: $BIN is not on your PATH. Add it, then open a new shell:"
  echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
fi

if [ "${1:-}" = "--run" ]; then
  echo
  # Read from the terminal so prompts work even under `curl ... | bash`.
  { : < /dev/tty; } 2>/dev/null && exec python3 "$DEST/slickstick.py" < /dev/tty
  exec python3 "$DEST/slickstick.py"
fi

echo
echo "Done. Start the wizard with:"
echo "  slickstick            # if ~/.local/bin is on your PATH"
echo "  python3 $DEST/slickstick.py"
