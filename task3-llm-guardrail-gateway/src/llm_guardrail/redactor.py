"""Streaming PII redaction.

``StreamRedactor`` receives text deltas as they arrive and returns the text that
is safe to send on. The difficulty is PII split across chunks: ``"john.doe@exa"``
followed by ``"mple.com"``. Sending the first chunk straight away would leak
half an email address.

The approach is to hold back only the **trailing fragment that could still grow
into a match**, and release everything before it immediately:

1. Append the delta to a small buffer.
2. Find the earliest position from which the buffer's end could still be part
   of PII: a run of email characters, or a run of digits and separators. That
   suffix is held. Everything before it is final and will not change.
3. Redact the final part and return it; keep the suffix in the buffer.
4. When the stream ends, ``flush()`` redacts and returns whatever is left.

Ordinary prose passes through with at most one partial word of delay, because a
space or punctuation mark ends the suffix. The buffer is capped at
``MAX_HOLDBACK`` characters, so memory stays bounded however long the stream
is. A fragment longer than that cannot be one of the supported patterns: email
addresses are limited to 254 characters, and card numbers and SSNs are far
shorter.
"""

import re
from collections.abc import Iterable

REDACTED = "[REDACTED]"
MAX_HOLDBACK = 256

EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
SSN = r"(?<!\d)\d{3}[- ]\d{2}[- ]\d{4}(?!\d)"
# 13-19 digits, optionally grouped with single spaces or hyphens.
CARD = r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"

PII_PATTERN = re.compile(f"(?P<email>{EMAIL})|(?P<ssn>{SSN})|(?P<card>{CARD})")

# Suffixes that could still become (part of) a match once more text arrives.
_EMAIL_TAIL = re.compile(r"[A-Za-z0-9._%+-]+(?:@[A-Za-z0-9.-]*)?\Z")
_DIGIT_TAIL = re.compile(r"\d(?:[ -]?\d)*[ -]?\Z")
_TAILS = (_EMAIL_TAIL, _DIGIT_TAIL)


def luhn_valid(number: str) -> bool:
    digits = [int(ch) for ch in number if ch.isdigit()]
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _replace(match: re.Match[str]) -> str:
    # Long digit runs that fail the Luhn check (order numbers, timestamps,
    # phone numbers) are left alone; only plausible card numbers are redacted.
    if match.lastgroup == "card" and not luhn_valid(match.group()):
        return match.group()
    return REDACTED


def redact(text: str) -> str:
    """Redact every complete match in ``text``."""
    return PII_PATTERN.sub(_replace, text)


def _held_suffix_start(buffer: str) -> int:
    """Index where the suffix that might still grow into PII begins."""
    start = len(buffer)
    for tail in _TAILS:
        match = tail.search(buffer)
        if match is not None:
            start = min(start, match.start())
    # Never cut through a complete match that reaches into the held suffix.
    for match in PII_PATTERN.finditer(buffer):
        if match.start() < start < match.end():
            start = match.start()
    return max(start, len(buffer) - MAX_HOLDBACK)


class StreamRedactor:
    """Redacts one text stream incrementally. Create one per stream (or choice)."""

    def __init__(self) -> None:
        self._buffer = ""
        self.max_buffered = 0  # largest buffer seen, for tests and metrics

    def feed(self, delta: str) -> str:
        """Add a delta; return the text that is now safe to send."""
        if not delta:
            return ""
        buffer = self._buffer + delta
        cut = _held_suffix_start(buffer)
        self._buffer = buffer[cut:]
        self.max_buffered = max(self.max_buffered, len(self._buffer))
        return redact(buffer[:cut])

    def flush(self) -> str:
        """Return the redacted remainder at the end of the stream."""
        remainder, self._buffer = self._buffer, ""
        return redact(remainder)

    @property
    def pending(self) -> int:
        return len(self._buffer)


def redact_stream(deltas: Iterable[str]) -> str:
    """Convenience: run a whole sequence of deltas through one redactor."""
    redactor = StreamRedactor()
    return "".join(redactor.feed(delta) for delta in deltas) + redactor.flush()
