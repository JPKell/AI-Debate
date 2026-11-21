"""
Purpose: Incremental streaming parser separating structured reasoning_content and inline <think> tags from answer text.
Role: util
Patterns: streaming-pipeline
Depends on: dataclasses, typing
Exposes: ParsedDelta, ThinkingParser
Tests: tests/test_thinking_parser.py
Invariants: Pure stateful text parser; handles arbitrarily split tag boundaries across SSE chunks without losing characters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OPEN_TAG = "<think>"
CLOSE_TAG = "</think>"


@dataclass(frozen=True)
class ParsedDelta:
    """Single normalized text chunk emitted by ThinkingParser."""

    kind: Literal["thinking", "content"]
    text: str


class ThinkingParser:
    """Stateful streaming parser normalizing reasoning_content and inline <think>...</think> blocks.

    Parameters:
        None.
    Returns:
        ThinkingParser instance.
    Side effects:
        Mutates internal tag-buffering state across feed/flush calls.
    """

    def __init__(self) -> None:
        self._in_think_tag = False
        self._buffer = ""

    def feed(
        self,
        *,
        content_delta: str | None = None,
        reasoning_delta: str | None = None,
    ) -> list[ParsedDelta]:
        """Consume one SSE delta and return zero or more normalized thinking/content chunks.

        Parameters:
            content_delta: Incremental string from delta.content, if present.
            reasoning_delta: Incremental string from delta.reasoning_content or delta.reasoning.
        Returns:
            Ordered list of ParsedDelta items ready for streaming or accumulation.
        Side effects:
            Updates internal parser buffer and tag state.
        """
        out: list[ParsedDelta] = []
        if reasoning_delta:
            out.append(ParsedDelta(kind="thinking", text=reasoning_delta))

        if not content_delta:
            return out

        self._buffer += content_delta
        while self._buffer:
            target_tag = CLOSE_TAG if self._in_think_tag else OPEN_TAG
            current_kind: Literal["thinking", "content"] = (
                "thinking" if self._in_think_tag else "content"
            )

            idx = self._buffer.find(target_tag)
            if idx != -1:
                if idx > 0:
                    out.append(ParsedDelta(kind=current_kind, text=self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(target_tag) :]
                self._in_think_tag = not self._in_think_tag
                continue

            # Hold back any trailing suffix that could be the start of target_tag.
            hold = self._longest_partial_prefix(self._buffer, target_tag)
            emit_len = len(self._buffer) - hold
            if emit_len > 0:
                out.append(ParsedDelta(kind=current_kind, text=self._buffer[:emit_len]))
                self._buffer = self._buffer[emit_len:]
            break

        return out

    def flush(self) -> list[ParsedDelta]:
        """Flush any buffered trailing characters at end-of-stream.

        Parameters:
            None.
        Returns:
            Remaining ParsedDelta items, if any.
        Side effects:
            Clears internal buffer.
        """
        if not self._buffer:
            return []
        kind: Literal["thinking", "content"] = "thinking" if self._in_think_tag else "content"
        text = self._buffer
        self._buffer = ""
        return [ParsedDelta(kind=kind, text=text)]

    @staticmethod
    def _longest_partial_prefix(text: str, tag: str) -> int:
        max_len = min(len(text), len(tag) - 1)
        for length in range(max_len, 0, -1):
            if text.endswith(tag[:length]):
                return length
        return 0
