.PHONY: check fmt lint test clean install-server install-client uninstall dev venv restart logs

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

# Create/sync virtual environment (requires uv)
venv:
	uv sync

# Install with dev dependencies (for development)
dev:
	uv sync --extra dev

# Server installation: runs the event bus service locally (idempotent)
# Use this on the machine that will host the event bus
# Re-run to pick up code changes (restarts service automatically)
install-server:
	@echo "Installing server..."
	uv sync
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
		$$CLAUDE_CMD mcp add --transport http --scope user agent-event-bus http://localhost:8080/mcp 2>/dev/null && \
			echo "Added agent-event-bus to Claude Code" || \
			echo "agent-event-bus already configured in Claude Code"; \
	else \
		echo "Note: claude not found. Run manually:"; \
		echo "  claude mcp add --transport http --scope user agent-event-bus http://localhost:8080/mcp"; \
	fi
	@echo ""
	@echo "Server installation complete!"
	@if ! echo "$$PATH" | tr ':' '\n' | grep -q "$$HOME/.local/bin"; then \
		echo ""; \
		echo "Make sure ~/.local/bin is in your PATH:"; \
		echo '  export PATH="$$HOME/.local/bin:$$PATH"'; \
	fi

# Client installation: connects to a remote event bus server (idempotent)
# Usage: make install-client REMOTE_URL=https://your-server.tailnet.ts.net/mcp
# Re-run to update remote URL or pick up CLI changes
install-client:
	@if [ -z "$(REMOTE_URL)" ]; then \
		echo "Error: REMOTE_URL is required"; \
		echo "Usage: make install-client REMOTE_URL=https://your-server.tailnet.ts.net/mcp"; \
		exit 1; \
	fi
	@echo "Installing client (connecting to $(REMOTE_URL))..."
	uv sync
	@echo ""
	@echo "Installing CLI..."
	./scripts/install-cli.sh
	@echo ""
	@echo "Configuring Claude Code MCP..."
	@CLAUDE_CMD=$$(command -v claude || echo "$$HOME/.local/bin/claude"); \
	if [ -x "$$CLAUDE_CMD" ]; then \
		$$CLAUDE_CMD mcp remove --scope user agent-event-bus 2>/dev/null || true; \
		$$CLAUDE_CMD mcp add --transport http --scope user agent-event-bus "$(REMOTE_URL)" && \
			echo "Added agent-event-bus to Claude Code ($(REMOTE_URL))"; \
	else \
		echo "Note: claude not found. Run manually:"; \
		echo "  claude mcp add --transport http --scope user agent-event-bus $(REMOTE_URL)"; \
	fi
	@echo ""
	@echo "Client installation complete!"
	@echo ""
	@echo "Add to your shell profile (~/.zshrc, ~/.bashrc, or ~/.extra):"
	@echo '  export AGENT_EVENT_BUS_URL="$(REMOTE_URL)"'

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
		$$CLAUDE_CMD mcp remove --scope user agent-event-bus 2>/dev/null && \
			echo "Removed agent-event-bus from Claude Code" || \
			echo "agent-event-bus not found in Claude Code"; \
	fi
	@echo ""
	@echo "Uninstall complete!"
	@echo "Note: venv and source code remain in place."

# Restart the server without dependency sync (faster than install-server)
restart:
	@if [ "$$(uname)" = "Darwin" ]; then \
		PLIST="$$HOME/Library/LaunchAgents/com.evansenter.agent-event-bus.plist"; \
		if [ -f "$$PLIST" ]; then \
			echo "Restarting agent-event-bus..."; \
			launchctl unload "$$PLIST" 2>/dev/null || true; \
			launchctl load "$$PLIST"; \
			sleep 1; \
			if launchctl list | grep -q "com.evansenter.agent-event-bus"; then \
				echo "Service restarted successfully"; \
			else \
				echo "Error: Service failed to start. Check ~/.claude/contrib/agent-event-bus/agent-event-bus.err"; \
				exit 1; \
			fi; \
		else \
			echo "LaunchAgent not installed. Run: make install-server"; \
			exit 1; \
		fi; \
	else \
		echo "Restarting agent-event-bus..."; \
		systemctl --user restart agent-event-bus; \
		sleep 1; \
		if systemctl --user is-active agent-event-bus &>/dev/null; then \
			echo "Service restarted successfully"; \
		else \
			echo "Error: Service failed to start. Check ~/.claude/contrib/agent-event-bus/agent-event-bus.err"; \
			exit 1; \
		fi; \
	fi

# Tail the event bus log
logs:
	@tail -f ~/.claude/contrib/agent-event-bus/agent-event-bus.log
