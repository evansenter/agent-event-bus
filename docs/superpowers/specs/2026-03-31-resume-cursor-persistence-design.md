# Fix: --resume flag silently drops events during rapid polling

**Issue:** [#114](https://github.com/evansenter/agent-event-bus/issues/114)
**Date:** 2026-03-31

## Problem

The `--resume` flag in `get_events` silently drops all events for sessions that haven't polled before. The early-return path (added in PR #100 to prevent flooding with historical events) returns `storage.get_cursor()` as `next_cursor` but never persists it to the session. Since `--resume` relies entirely on server-side cursor tracking, the session is permanently stuck with no cursor, and every subsequent poll returns empty.

## Root Cause

In `server.py`'s `get_events` function, the resume early-return `else` branch handles two distinct cases identically:
1. Session doesn't exist (`session` is `None`)
2. Session exists but has no saved cursor (`session.last_cursor` is falsy)

Neither case persists the tip cursor via `update_session_cursor()`.

## Fix

Split the branch into three paths:

1. **Session exists with cursor** (existing, working): use `session.last_cursor` as query cursor
2. **Session exists without cursor** (new): persist tip cursor via `update_session_cursor()`, return empty events. Next `resume=True` poll starts from this cursor and picks up new events.
3. **Session doesn't exist** (new): return `{"error": "Session not found", "session_id": ...}` following the existing error pattern used in `unregister_session`

## Files to Change

- `src/agent_event_bus/server.py` — split the early-return branch in `get_events` (lines 401-415)
- `tests/test_server.py` — add regression tests, update existing test for new error response

## Tests

1. **`test_resume_persists_tip_cursor_for_cursorless_session`**: Register session, publish events, first `resume=True` returns empty (tip persisted), publish new event, second `resume=True` returns only the new event.
2. **`test_resume_with_nonexistent_session_returns_error`**: Update existing test to expect `{"error": "Session not found"}` instead of empty events.
3. **`test_rapid_resume_polling_no_event_loss`**: Register, publish N events, poll with resume multiple times, verify no events are lost across polling cycles.
