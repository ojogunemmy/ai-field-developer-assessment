import random

import pytest

from llm_guardrail.redactor import (
    MAX_HOLDBACK,
    REDACTED,
    StreamRedactor,
    luhn_valid,
    redact,
    redact_stream,
)

VISA = "4111 1111 1111 1111"  # Luhn-valid test number
SAMPLES = [
    "Contact john.doe@example.com for details.",
    "My SSN is 123-45-6789, keep it safe.",
    f"Card {VISA} expires 12/29.",
    "Card 4111-1111-1111-1111 on file.",
    "Pay with 4111111111111111 today",
    "Two emails: a@b.io and first.last+tag@sub.example.co.uk!",
    "Mixed 123 45 6789 and ann@corp.org and 5500 0000 0000 0004.",
    "Ends with an email ann@corp.org",
    "Ends with a card 4111111111111111",
    "SSN at the very end 123-45-6789",
    "email@start.com is first",
]
SECRETS = [
    "john.doe@example.com",
    "123-45-6789",
    VISA,
    "4111-1111-1111-1111",
    "4111111111111111",
    "a@b.io",
    "first.last+tag@sub.example.co.uk",
    "123 45 6789",
    "ann@corp.org",
    "5500 0000 0000 0004",
    "email@start.com",
]


# --- one-shot redaction ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail me at bob@example.com", f"mail me at {REDACTED}"),
        ("ssn 123-45-6789.", f"ssn {REDACTED}."),
        (f"visa {VISA}", f"visa {REDACTED}"),
        ("no pii here", "no pii here"),
        ("order 1234567812345678 shipped", "order 1234567812345678 shipped"),  # fails Luhn
        ("call 555-123-4567", "call 555-123-4567"),  # phone number, not an SSN
        ("1234-56-7890", "1234-56-7890"),  # too many leading digits for an SSN
        ("year 2024 and 42 apples", "year 2024 and 42 apples"),
        ("not an email: user@localhost", "not an email: user@localhost"),
    ],
)
def test_redact(text, expected):
    assert redact(text) == expected


def test_luhn():
    assert luhn_valid("4111111111111111")
    assert luhn_valid("5500 0000 0000 0004")
    assert not luhn_valid("4111111111111112")


# --- streaming equals one-shot, for every split -------------------------


@pytest.mark.parametrize("text", SAMPLES)
def test_every_single_split_point(text):
    expected = redact(text)
    for cut in range(len(text) + 1):
        assert redact_stream([text[:cut], text[cut:]]) == expected, f"split at {cut}"


@pytest.mark.parametrize("text", SAMPLES)
def test_character_by_character(text):
    assert redact_stream(list(text)) == redact(text)


@pytest.mark.parametrize("seed", range(200))
def test_random_chunking(seed):
    rng = random.Random(seed)
    text = " ".join(rng.choice(SAMPLES) for _ in range(5))
    chunks, position = [], 0
    while position < len(text):
        size = rng.randint(1, 12)
        chunks.append(text[position : position + size])
        position += size
    assert redact_stream(chunks) == redact(text)


@pytest.mark.parametrize("text", SAMPLES)
def test_no_secret_fragment_is_ever_emitted(text):
    """Nothing emitted mid-stream may contain a secret, even partially joined."""
    for cut in range(1, len(text)):
        redactor = StreamRedactor()
        emitted = redactor.feed(text[:cut])
        for secret in SECRETS:
            # A secret must never appear in released text in full...
            assert secret not in emitted
            # ...and released text must not end with the start of a secret that
            # the rest of the stream would complete.
            if secret in text:
                position = text.index(secret)
                if position < cut < position + len(secret):
                    assert not emitted.endswith(text[position:cut])


# --- latency and memory ---------------------------------------------------


def test_prose_is_released_promptly():
    redactor = StreamRedactor()
    assert redactor.feed("Hello there, ") == "Hello there, "
    # Only the trailing partial word is held back.
    assert redactor.feed("how are yo") == "how are "
    assert redactor.feed("u?") == "you?"


def test_partial_email_is_held_until_resolved():
    redactor = StreamRedactor()
    assert redactor.feed("write to jane") == "write to "
    assert redactor.feed("@exam") == ""
    assert redactor.feed("ple.com now") == f"{REDACTED} "
    assert redactor.flush() == "now"


def test_buffer_stays_bounded_on_long_streams():
    rng = random.Random(7)
    redactor = StreamRedactor()
    total = 0
    for _ in range(50_000):  # about 1 MB of mixed text and PII
        piece = rng.choice(SAMPLES) + " "
        redactor.feed(piece)
        total += len(piece)
    assert total > 1_000_000
    assert redactor.max_buffered <= MAX_HOLDBACK


def test_unbroken_token_is_capped_not_buffered_forever():
    redactor = StreamRedactor()
    released = "".join(redactor.feed("a" * 100) for _ in range(20))
    assert redactor.pending <= MAX_HOLDBACK
    assert len(released) + redactor.pending == 2000


def test_empty_delta():
    redactor = StreamRedactor()
    assert redactor.feed("") == ""
    assert redactor.flush() == ""
