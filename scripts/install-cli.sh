#!/bin/bash
# Install event-bus-cli to ~/.local/bin for use in shell scripts and hooks

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
INSTALL_DIR="$HOME/.local/bin"
CLI_PATH="$INSTALL_DIR/event-bus-cli"

# Check venv exists
if [[ ! -f "$VENV_PYTHON" ]]; then
    echo "Error: Virtual environment not found at $PROJECT_DIR/.venv"
    echo "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e ."
    exit 1
fi

# Create install directory
mkdir -p "$INSTALL_DIR"

# Create wrapper script
cat > "$CLI_PATH" << EOF
#!/bin/bash
# event-bus-cli wrapper - installed by claude-event-bus
# Source: $PROJECT_DIR
exec "$VENV_PYTHON" -m event_bus.cli "\$@"
EOF

chmod +x "$CLI_PATH"

echo "Installed event-bus-cli to $CLI_PATH"

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
