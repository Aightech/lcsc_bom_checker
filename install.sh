#!/usr/bin/env bash
# install.sh — user-only installation of lcsc_bom_checker

set -euo pipefail

SCRIPT_NAME="lcsc_bom_checker"
TARGET_DIR="$HOME/.local/bin"

echo "Installing $SCRIPT_NAME into $TARGET_DIR ..."

directory=$(pwd)
echo "Current directory: $directory"

# transform "--cache", default=".lcsc_cache" to "--cache, default=${directory}/.lcsc_cache"
sed "s|cache\", default=\".lcsc_cache\"|cache\", default=\"$(pwd)/.lcsc_cache\"|g" lcsc_bom_checkerC.py > lcsc_bom_checker.py

# Ensure ~/.local/bin exists
mkdir -p "$TARGET_DIR"

# Copy script and drop .py extension
cp -f lcsc_bom_checker.py "$TARGET_DIR/$SCRIPT_NAME"

# Make sure it’s executable
chmod +x "$TARGET_DIR/$SCRIPT_NAME"

# Ensure ~/.local/bin is in PATH
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo "Warning: $TARGET_DIR is not in your PATH."
    echo "Add this line to your ~/.bashrc or ~/.zshrc:"
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo "Done. You can now run '$SCRIPT_NAME' from anywhere."
