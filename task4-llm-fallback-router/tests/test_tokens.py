import pytest

from llm_router.tokens import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    actual_usage,
    completion_budget,
    estimate_prompt_tokens,
    reservation_for,
)


def test_prompt_estimate_grows_with_text():
    short = estimate_prompt_tokens([{"role": "user", "content": "hi"}])
    long = estimate_prompt_tokens([{"role": "user", "content": "hi " * 400}])
    assert long > short + 250


def test_multipart_content_counts():
    parts = [{"role": "user", "content": [{"type": "text", "text": "a" * 400}]}]
    assert estimate_prompt_tokens(parts) == 3 + 4 + 100


@pytest.mark.parametrize(
    ("payload", "budget"),
    [
        ({}, DEFAULT_MAX_COMPLETION_TOKENS),
        ({"max_tokens": 50}, 50),
        ({"max_completion_tokens": 70, "max_tokens": 50}, 70),
        ({"max_tokens": 0}, DEFAULT_MAX_COMPLETION_TOKENS),
        ({"max_tokens": True}, DEFAULT_MAX_COMPLETION_TOKENS),
    ],
)
def test_completion_budget(payload, budget):
    assert completion_budget(payload) == budget


def test_reservation_is_prompt_plus_budget():
    payload = {"messages": [{"role": "user", "content": "a" * 40}], "max_tokens": 100}
    assert reservation_for(payload) == 3 + 4 + 10 + 100


def test_actual_usage_prefers_provider_count():
    payload = {"messages": [{"role": "user", "content": "x"}]}
    assert actual_usage({"usage": {"total_tokens": 321}, "choices": []}, payload) == 321


def test_actual_usage_estimates_when_missing():
    payload = {"messages": [{"role": "user", "content": "a" * 40}]}
    response = {"choices": [{"message": {"content": "b" * 80}}]}
    assert actual_usage(response, payload) == (3 + 4 + 10) + 20
