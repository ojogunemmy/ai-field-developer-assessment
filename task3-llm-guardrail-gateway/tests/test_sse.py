import pytest

from llm_guardrail.sse import SSEParser, format_event

STREAM = (
    b": keep-alive comment\n\n"
    b'data: {"a": 1}\n\n'
    b'data: {"b": "caf\xc3\xa9"}\r\n\r\n'
    b"event: message\ndata: line one\ndata: line two\n\n"
    b"data: [DONE]\n\n"
)
EXPECTED = ['{"a": 1}', '{"b": "café"}', "line one\nline two", "[DONE]"]


def parse_all(chunks):
    parser = SSEParser()
    events = [event for chunk in chunks for event in parser.feed(chunk)]
    return events + list(parser.close())


def test_whole_stream():
    assert parse_all([STREAM]) == EXPECTED


@pytest.mark.parametrize("cut", range(len(STREAM) + 1))
def test_every_byte_split(cut):
    # Includes splits inside the two-byte "é", which must be reassembled.
    assert parse_all([STREAM[:cut], STREAM[cut:]]) == EXPECTED


def test_byte_by_byte():
    assert parse_all([STREAM[i : i + 1] for i in range(len(STREAM))]) == EXPECTED


def test_unterminated_final_event_is_emitted_on_close():
    assert parse_all([b"data: tail"]) == ["tail"]


def test_format_event():
    assert format_event("x") == b"data: x\n\n"
