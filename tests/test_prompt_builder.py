"""
Purpose: Unit tests for prompt composition and A/B role-flipping history builder.
Role: test
Patterns: contract-first-test
Depends on: app.services.prompt_builder, app.schemas
Exposes: test_compose_system_prompt_includes_style_conviction_side, test_turn_one_opening_message, test_role_flipping_and_thinking_exclusion, test_moderator_interjection_formatting
Tests: tests/test_prompt_builder.py
Invariants: Thinking text must never appear in messages sent to either model.
"""

from __future__ import annotations

from app.schemas import DebateRecord, MessageRecord, ModelParticipantConfig
from app.services.prompt_builder import build_messages_for_speaker, compose_system_prompt


def _make_debate() -> DebateRecord:
    return DebateRecord(
        id="deb-1",
        title="Tabs vs Spaces",
        scene_prompt="Debate whether tabs or spaces are better for indentation.",
        first_speaker="A",
        resolved_first_speaker="A",
        status="running",
        turn_limit=6,
        time_limit_seconds=None,
        elapsed_active_seconds=0.0,
        show_thinking=True,
        model_a_config=ModelParticipantConfig(
            model_id="model-a",
            label="Alpha",
            style="adversarial",
            conviction="immovable",
            side="Tabs are strictly superior for accessibility.",
        ),
        model_b_config=ModelParticipantConfig(
            model_id="model-b",
            label="Beta",
            style="cooperative",
            conviction="flexible",
            side="Spaces guarantee identical alignment everywhere.",
        ),
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


def test_compose_system_prompt_includes_style_conviction_side() -> None:
    debate = _make_debate()
    prompt = compose_system_prompt(
        participant=debate.model_a_config,
        opponent_label=debate.model_b_config.effective_label("Model B"),
        scene_prompt=debate.scene_prompt,
    )
    assert "Alpha" in prompt
    assert "Beta" in prompt
    assert "Tabs are strictly superior" in prompt
    assert "Debate whether tabs or spaces" in prompt
    assert "never change" in prompt.lower() or "immovable" in prompt.lower()


def test_turn_one_opening_message() -> None:
    debate = _make_debate()
    msgs = build_messages_for_speaker(debate=debate, history=[], speaker="A")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "opening" in msgs[1]["content"].lower()


def test_role_flipping_and_thinking_exclusion() -> None:
    debate = _make_debate()
    history = [
        MessageRecord(
            id="m1",
            debate_id="deb-1",
            turn_index=1,
            sender="A",
            model_id="model-a",
            content="Tabs adapt to each reader's width.",
            thinking="SECRET_A_THINKING_DO_NOT_LEAK",
            created_at="2026-01-01T00:00:01Z",
        ),
        MessageRecord(
            id="m2",
            debate_id="deb-1",
            turn_index=2,
            sender="B",
            model_id="model-b",
            content="Spaces never break ASCII diagrams.",
            thinking="SECRET_B_THINKING_DO_NOT_LEAK",
            created_at="2026-01-01T00:00:02Z",
        ),
    ]

    msgs_for_a = build_messages_for_speaker(debate=debate, history=history, speaker="A")
    # Strict system -> user -> assistant -> user alternation (opening user prompt inserted before A's first turn)
    assert [m["role"] for m in msgs_for_a] == ["system", "user", "assistant", "user"]
    assert msgs_for_a[2]["content"] == "Tabs adapt to each reader's width."
    assert msgs_for_a[3]["content"] == "Spaces never break ASCII diagrams."

    msgs_for_b = build_messages_for_speaker(debate=debate, history=history[:1], speaker="B")
    assert [m["role"] for m in msgs_for_b] == ["system", "user"]
    assert msgs_for_b[1]["content"] == "Tabs adapt to each reader's width."

    serialized = str(msgs_for_a) + str(msgs_for_b)
    assert "SECRET_A_THINKING_DO_NOT_LEAK" not in serialized
    assert "SECRET_B_THINKING_DO_NOT_LEAK" not in serialized


def test_moderator_interjection_formatting() -> None:
    debate = _make_debate()
    history = [
        MessageRecord(
            id="m1",
            debate_id="deb-1",
            turn_index=1,
            sender="A",
            model_id="model-a",
            content="Opening from A.",
            created_at="2026-01-01T00:00:01Z",
        ),
        MessageRecord(
            id="m2",
            debate_id="deb-1",
            turn_index=2,
            sender="moderator",
            content="Address Python PEP 8 specifically.",
            created_at="2026-01-01T00:00:02Z",
        ),
    ]
    msgs_for_b = build_messages_for_speaker(debate=debate, history=history, speaker="B")
    assert msgs_for_b[-1]["role"] == "user"
    assert "[Moderator]: Address Python PEP 8 specifically." in msgs_for_b[-1]["content"]
