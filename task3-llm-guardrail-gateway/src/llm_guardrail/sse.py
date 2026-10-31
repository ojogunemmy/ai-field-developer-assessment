"""Incremental Server-Sent Events parsing.

Network reads split the byte stream at arbitrary points: in the middle of a
line, an event, or even a multi-byte UTF-8 character. ``SSEParser`` keeps only
the incomplete tail between calls, so memory use is bounded by the largest
single event, not by the length of the stream.
"""

import codecs
from collections.abc import Iterator


class SSEParser:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._line_buffer = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes) -> Iterator[str]:
        """Yield the ``data`` payload of each event completed by ``chunk``."""
        self._line_buffer += self._decoder.decode(chunk)
        while True:
            newline = self._line_buffer.find("\n")
            if newline == -1:
                return
            line = self._line_buffer[:newline].removesuffix("\r")
            self._line_buffer = self._line_buffer[newline + 1 :]
            event = self._process_line(line)
            if event is not None:
                yield event

    def close(self) -> Iterator[str]:
        """Yield a final event if the stream ended without a blank line."""
        self._line_buffer += self._decoder.decode(b"", final=True)
        if self._line_buffer:
            event = self._process_line(self._line_buffer)
            self._line_buffer = ""
            if event is not None:
                yield event
        if self._data_lines:
            yield "\n".join(self._data_lines)
            self._data_lines = []

    def _process_line(self, line: str) -> str | None:
        if line == "":
            if not self._data_lines:
                return None
            data = "\n".join(self._data_lines)
            self._data_lines = []
            return data
        if line.startswith(":"):
            return None  # comment / keep-alive
        field, _, value = line.partition(":")
        if field == "data":
            self._data_lines.append(value.removeprefix(" "))
        return None


def format_event(data: str) -> bytes:
    return f"data: {data}\n\n".encode()
