"""Token estimation.

A request's real token count is only known after the provider answers, but the
rate limiter must decide *before* sending it. So the router reserves a
pessimistic estimate up front (prompt estimate + the completion budget) and
later corrects the reservation to the provider's reported ``usage``.

The prompt estimate uses the common rule of thumb of ~4 characters per token,
plus a small per-message overhead for role and formatting tokens. It does not
need to be exact: it is replaced by the real count after the call.
"""

import math
from typing import Any

CHARS_PER_TOKEN = 4
TOKENS_PER_MESSAGE = 4
REPLY_PRIMING_TOKENS = 3
DEFAULT_MAX_COMPLETION_TOKENS = 1024


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multi-part content: [{"type": "text", "text": ...}]
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def estimate_text_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_prompt_tokens(messages: list[Any]) -> int:
    total = REPLY_PRIMING_TOKENS
    for message in messages:
        total += TOKENS_PER_MESSAGE
        if isinstance(message, dict):
            total += estimate_text_tokens(_text_of(message.get("content")))
    return total


def completion_budget(payload: dict[str, Any]) -> int:
    for key in ("max_completion_tokens", "max_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return DEFAULT_MAX_COMPLETION_TOKENS


def reservation_for(payload: dict[str, Any]) -> int:
    """Tokens to reserve before the call: worst case for this request."""
    return estimate_prompt_tokens(payload["messages"]) + completion_budget(payload)


def actual_usage(response: dict[str, Any], payload: dict[str, Any]) -> int:
    """Tokens the call really used: the provider's count, or an estimate."""
    usage = response.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            return total
    completion_text = ""
    for choice in response.get("choices") or []:
        if isinstance(choice, dict) and isinstance(choice.get("message"), dict):
            completion_text += _text_of(choice["message"].get("content"))
    return estimate_prompt_tokens(payload["messages"]) + estimate_text_tokens(completion_text)
