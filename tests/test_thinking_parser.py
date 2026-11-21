"""
Purpose: Unit tests for streaming ThinkingParser handling reasoning_content and inline think tags.
Role: test
Patterns: contract-first-test, streaming-pipeline
Depends on: app.services.thinking_parser
Exposes: test_structured_reasoning_and_content, test_inline_think_tags_single_chunk, test_inline_think_tags_split_across_chunks, test_no_think_tags_passthrough, test_unclosed_think_tag_flush, test_mixed_reasoning_and_empty_think_tags
Tests: tests/test_thinking_parser.py
Invariants: All tests are pure, deterministic, and require no network or filesystem access.
"""

from __future__ import annotations

from app.services.thinking_parser import ThinkingParser


def test_structured_reasoning_and_content() -> None:
    parser = ThinkingParser()
    events = []
    events.extend(parser.feed(reasoning_delta="Step 1. "))
    events.extend(parser.feed(reasoning_delta="Step 2."))
    events.extend(parser.feed(content_delta="Final answer."))
    events.extend(parser.flush())

    thinking = "".join(e.text for e in events if e.kind == "thinking")
    content = "".join(e.text for e in events if e.kind == "content")
    assert thinking == "Step 1. Step 2."
    assert content == "Final answer."


def test_inline_think_tags_single_chunk() -> None:
    parser = ThinkingParser()
    events = parser.feed(content_delta="<think>Private chain</think>\nPublic answer.")
    events.extend(parser.flush())

    thinking = "".join(e.text for e in events if e.kind == "thinking")
    content = "".join(e.text for e in events if e.kind == "content")
    assert thinking == "Private chain"
    assert content.strip() == "Public answer."


def test_inline_think_tags_split_across_chunks() -> None:
    parser = ThinkingParser()
    chunks = ["<thi", "nk>First ", "thought</th", "ink>Hello ", "world!"]
    events = []
    for chunk in chunks:
        events.extend(parser.feed(content_delta=chunk))
    events.extend(parser.flush())

    thinking = "".join(e.text for e in events if e.kind == "thinking")
    content = "".join(e.text for e in events if e.kind == "content")
    assert thinking == "First thought"
    assert content == "Hello world!"


def test_no_think_tags_passthrough() -> None:
    parser = ThinkingParser()
    events = parser.feed(content_delta="2 < 3 and 5 > 4.")
    events.extend(parser.flush())

    thinking = "".join(e.text for e in events if e.kind == "thinking")
    content = "".join(e.text for e in events if e.kind == "content")
    assert thinking == ""
    assert content == "2 < 3 and 5 > 4."


def test_unclosed_think_tag_flush() -> None:
    parser = ThinkingParser()
    events = parser.feed(content_delta="<think>Cut off mid-thought")
    events.extend(parser.flush())

    thinking = "".join(e.text for e in events if e.kind == "thinking")
    content = "".join(e.text for e in events if e.kind == "content")
    assert thinking == "Cut off mid-thought"
    assert content == ""


def test_mixed_reasoning_and_empty_think_tags() -> None:
    parser = ThinkingParser()
    events = []
    events.extend(parser.feed(reasoning_delta="Structured thought."))
    events.extend(parser.feed(content_delta="<think></think>Actual response."))
    events.extend(parser.flush())

    thinking = "".join(e.text for e in events if e.kind == "thinking")
    content = "".join(e.text for e in events if e.kind == "content")
    assert thinking == "Structured thought."
    assert content == "Actual response."

