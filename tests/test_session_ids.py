"""Tests for session_ids module (dinosaur-themed display ID generation)."""

from agent_event_bus.middleware import _is_human_readable_id
from agent_event_bus.session_ids import ADJECTIVES, DINOSAURS, generate_session_id


class TestGenerateSessionId:
    """Tests for generate_session_id function."""

    def test_returns_adjective_noun_format(self):
        """Generated ID has format: adjective-noun."""
        session_id = generate_session_id()
        parts = session_id.split("-")
        assert len(parts) == 2, f"Expected 2 parts, got {len(parts)}: {session_id}"
        assert parts[0].isalpha() and parts[0].islower()
        assert parts[1].isalpha() and parts[1].islower()

    def test_passes_human_readable_check(self):
        """Generated ID passes middleware's _is_human_readable_id check."""
        session_id = generate_session_id()
        assert _is_human_readable_id(session_id), f"ID failed check: {session_id}"

    def test_uses_words_from_lists(self):
        """Generated ID uses words from ADJECTIVES and DINOSAURS lists."""
        session_id = generate_session_id()
        adjective, dinosaur = session_id.split("-")
        assert adjective in ADJECTIVES, f"Adjective not in list: {adjective}"
        assert dinosaur in DINOSAURS, f"Dinosaur not in list: {dinosaur}"

    def test_randomness(self):
        """Multiple calls produce different results (with high probability)."""
        # Generate 10 IDs - statistically very unlikely to all be the same
        # (1 in 34^2 * 10 = 1 in 11,560 chance of collision per pair)
        ids = {generate_session_id() for _ in range(10)}
        assert len(ids) > 1, "All 10 generated IDs were identical"

    def test_word_lists_non_empty(self):
        """Word lists have sufficient entries for good randomness."""
        assert len(ADJECTIVES) >= 10, "ADJECTIVES list too small"
        assert len(DINOSAURS) >= 10, "DINOSAURS list too small"

    def test_word_lists_lowercase(self):
        """All words in lists are lowercase alphabetic."""
        for word in ADJECTIVES:
            assert word.isalpha() and word.islower(), f"Invalid adjective: {word}"
        for word in DINOSAURS:
            assert word.isalpha() and word.islower(), f"Invalid dinosaur: {word}"
