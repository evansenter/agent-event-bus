"""Human-readable session ID generation (Docker-style names)."""

import random

# Word lists for human-readable session IDs
ADJECTIVES = [
    "brave",
    "calm",
    "clever",
    "eager",
    "fancy",
    "gentle",
    "happy",
    "jolly",
    "keen",
    "lively",
    "merry",
    "nice",
    "polite",
    "quick",
    "sharp",
    "swift",
    "tender",
    "upbeat",
    "vivid",
    "warm",
    "witty",
    "zesty",
    "bold",
    "bright",
    "crisp",
    "daring",
    "epic",
    "fresh",
    "grand",
    "humble",
    "jovial",
    "kind",
]

ANIMALS = [
    "badger",
    "cat",
    "dog",
    "eagle",
    "falcon",
    "gopher",
    "heron",
    "ibis",
    "jaguar",
    "koala",
    "lemur",
    "moose",
    "newt",
    "otter",
    "panda",
    "quail",
    "rabbit",
    "salmon",
    "tiger",
    "urchin",
    "viper",
    "walrus",
    "yak",
    "zebra",
    "bear",
    "crane",
    "duck",
    "fox",
    "goose",
    "hawk",
    "iguana",
    "jay",
]


def generate_session_id() -> str:
    """Generate a human-readable session ID like 'brave-tiger'."""
    return f"{random.choice(ADJECTIVES)}-{random.choice(ANIMALS)}"
