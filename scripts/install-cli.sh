#!/bin/bash
# Install event-bus-cli to ~/.local/bin as a symlink

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_CLI="$PROJECT_DIR/.venv/bin/event-bus-cli"
INSTALL_DIR="$HOME/.local/bin"
CLI_PATH="$INSTALL_DIR/event-bus-cli"

# Check venv CLI exists
if [[ ! -f "$VENV_CLI" ]]; then
    echo "Error: CLI not found at $VENV_CLI"
    echo "Run: make dev (or pip install -e .)"
    exit 1
fi

# Create install directory
mkdir -p "$INSTALL_DIR"

# Remove existing file/symlink if present
if [[ -e "$CLI_PATH" || -L "$CLI_PATH" ]]; then
    # Skip if already correctly symlinked
    if [[ -L "$CLI_PATH" && "$(readlink "$CLI_PATH")" == "$VENV_CLI" ]]; then
        echo "event-bus-cli already symlinked correctly"
        exit 0
    fi
    rm -f "$CLI_PATH"
fi

# Create symlink
ln -s "$VENV_CLI" "$CLI_PATH"
echo "Installed event-bus-cli to $CLI_PATH (symlink)"

# Check if ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    echo ""
    echo "Warning: $INSTALL_DIR is not in your PATH"
    echo "Add this to your shell profile (.bashrc, .zshrc, etc.):"
    echo ""
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi

# Test it works
if "$CLI_PATH" --help > /dev/null 2>&1; then
    echo "Verified: event-bus-cli is working"
else
    echo "Warning: event-bus-cli installed but test failed"
    exit 1
fi
