.PHONY: check fmt lint test clean install install-cli uninstall dev venv restart reinstall logs

# Run all quality gates (format check, lint, tests)
check: fmt lint test

# Check/fix formatting with ruff
fmt:
	ruff format --check .

# Run linter with ruff
lint:
	ruff check .

# Run tests
test:
	pytest tests/ -v

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Create virtual environment (requires Python 3.10+)
venv:
	@if [ ! -d .venv ]; then \
		echo "Creating virtual environment..."; \
		PYTHON=$$(command -v python3.12 || command -v python3.11 || command -v python3.10 || echo "python3"); \
		$$PYTHON -m venv .venv && .venv/bin/pip install --upgrade pip; \
	fi

# Install with dev dependencies (for development)
dev: venv
	.venv/bin/pip install -e ".[dev]"

# Full installation: venv + deps + service + CLI + MCP
install: venv
	@echo "Installing dependencies..."
	.venv/bin/pip install -e .
	@echo ""
	@if [ "$$(uname)" = "Darwin" ]; then \
		echo "Installing LaunchAgent (macOS)..."; \
		./scripts/install-launchagent.sh; \
	else \
		echo "Installing systemd service (Linux)..."; \
		./scripts/install-systemd.sh; \
	fi
	@echo ""
	@echo "Adding to Claude Code..."
	@CLAUDE_CMD=$$(command -v claude || echo "$$HOME/.local/bin/claude"); \
	if [ -x "$$CLAUDE_CMD" ]; then \
		$$CLAUDE_CMD mcp add --transport http --scope user event-bus http://localhost:8080/mcp 2>/dev/null && \
			echo "Added event-bus to Claude Code" || \
			echo "event-bus already configured in Claude Code"; \
	else \
		echo "Note: claude not found. Run manually:"; \
		echo "  claude mcp add --transport http --scope user event-bus http://localhost:8080/mcp"; \
	fi
	@echo ""
	@echo "Installation complete!"
	@echo ""
	@echo "Make sure ~/.local/bin is in your PATH:"
	@echo '  export PATH="$$HOME/.local/bin:$$PATH"'

# CLI-only installation (for remote setups - no local server)
install-cli: venv
	@echo "Installing dependencies..."
	.venv/bin/pip install -e .
	@echo ""
	@echo "Installing CLI..."
	./scripts/install-cli.sh
	@echo ""
	@echo "CLI-only installation complete!"
	@echo ""
	@echo "For remote event bus, set in your shell profile:"
	@echo '  export EVENT_BUS_URL="http://<server-ip>:8080/mcp"'
	@echo ""
	@echo "And add MCP server to Claude Code:"
	@echo '  claude mcp add --transport http --scope user event-bus http://<server-ip>:8080/mcp'

# Uninstall: service + CLI + MCP config
uninstall:
	@echo "Uninstalling..."
	@if [ "$$(uname)" = "Darwin" ]; then \
		./scripts/uninstall-launchagent.sh; \
	else \
		./scripts/uninstall-systemd.sh; \
	fi
	@echo ""
	@echo "Removing from Claude Code..."
	@CLAUDE_CMD=$$(command -v claude || echo "$$HOME/.local/bin/claude"); \
	if [ -x "$$CLAUDE_CMD" ]; then \
		$$CLAUDE_CMD mcp remove --scope user event-bus 2>/dev/null && \
			echo "Removed event-bus from Claude Code" || \
			echo "event-bus not found in Claude Code"; \
	fi
	@echo ""
	@echo "Uninstall complete!"
	@echo "Note: venv and source code remain in place."

# Restart the server (reload code changes)
restart:
	@if [ "$$(uname)" = "Darwin" ]; then \
		PLIST="$$HOME/Library/LaunchAgents/com.evansenter.claude-event-bus.plist"; \
		if [ -f "$$PLIST" ]; then \
			echo "Restarting event-bus..."; \
			launchctl unload "$$PLIST" 2>/dev/null || true; \
			launchctl load "$$PLIST"; \
			sleep 1; \
			if launchctl list | grep -q "com.evansenter.claude-event-bus"; then \
				echo "Service restarted successfully"; \
			else \
				echo "Error: Service failed to start. Check ~/.claude/contrib/event-bus/event-bus.err"; \
				exit 1; \
			fi; \
		else \
			echo "LaunchAgent not installed. Run: make install"; \
			exit 1; \
		fi; \
	else \
		echo "Restarting event-bus..."; \
		systemctl --user restart claude-event-bus; \
		sleep 1; \
		if systemctl --user is-active claude-event-bus &>/dev/null; then \
			echo "Service restarted successfully"; \
		else \
			echo "Error: Service failed to start. Check ~/.claude/contrib/event-bus/event-bus.err"; \
			exit 1; \
		fi; \
	fi

# Reinstall and restart (install + restart in one command)
reinstall: install restart

# Tail the event bus log
logs:
	@tail -f ~/.claude/contrib/event-bus/event-bus.log
