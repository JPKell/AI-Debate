"""
Purpose: Unit tests for prompt composition, roleplay/dialogue rules, narrator prompting, and A/B role-flipping history builder.
Role: test
Patterns: contract-first-test
Depends on: app.services.prompt_builder, app.schemas
Exposes: test_compose_system_prompt_includes_style_conviction_side, test_turn_one_opening_message, test_role_flipping_and_thinking_exclusion, test_moderator_interjection_formatting, test_roleplay_dialogue_constraints_and_narrator_prompts, test_characters_receive_narrator_reaction_instructions_and_cue
Tests: tests/test_prompt_builder.py
Invariants: Thinking text must never appear in messages sent to any model (A, B, or Narrator).
"""

from __future__ import annotations

from app.schemas import DebateRecord, MessageRecord, ModelParticipantConfig, NarratorConfig
from app.services.prompt_builder import (
    build_messages_for_speaker,
    compose_narrator_system_prompt,
    compose_system_prompt,
)


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


def test_roleplay_dialogue_constraints_and_narrator_prompts() -> None:
    debate = DebateRecord(
        id="deb-rp",
        title="Tavern Standoff",
        scene_prompt="Two rival detectives meet in a rain-soaked tavern.",
        first_speaker="A",
        resolved_first_speaker="A",
        status="running",
        turn_limit=4,
        narrator_enabled=True,
        narrator_config=NarratorConfig(model_id="model-narrator", label="Scene Narrator"),
        narrator_cadence_mode="every_turn",
        narrator_cadence_n=1,
        model_a_config=ModelParticipantConfig(
            model_id="model-a",
            label="Detective Vance",
            style="adversarial",
            conviction="firm",
            side="Insist the ledger belongs to the precinct.",
            dialogue_only=True,
            no_thoughts_as_actions=True,
            no_self_narration=True,
        ),
        model_b_config=ModelParticipantConfig(
            model_id="model-b",
            label="Inspector Mara",
            style="explorative",
            conviction="balanced",
            side="Protect the witness list inside the ledger.",
            dialogue_only=True,
            no_thoughts_as_actions=True,
        ),
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )

    sys_a = compose_system_prompt(
        participant=debate.model_a_config,
        opponent_label="Inspector Mara",
        scene_prompt=debate.scene_prompt,
        narrator_enabled=True,
    )
    assert "directly to 'Inspector Mara' only" in sys_a
    assert "internal thoughts" in sys_a.lower()
    assert "narrator" in sys_a.lower()

    nar_sys = compose_narrator_system_prompt(debate)
    assert "Detective Vance" in nar_sys
    assert "Inspector Mara" in nar_sys
    assert "internal thoughts" in nar_sys.lower()

    history = [
        MessageRecord(
            id="m1",
            debate_id="deb-rp",
            turn_index=1,
            sender="A",
            model_id="model-a",
            content="Put the ledger on the table, Mara.",
            thinking="SECRET_VANCE_THOUGHT",
            created_at="2026-01-01T00:00:01Z",
        ),
        MessageRecord(
            id="m2",
            debate_id="deb-rp",
            turn_index=2,
            sender="narrator",
            model_id="model-narrator",
            content="Vance leans forward as rain lashes the tavern window.",
            thinking="SECRET_NARRATOR_THOUGHT",
            created_at="2026-01-01T00:00:02Z",
        ),
        MessageRecord(
            id="m3",
            debate_id="deb-rp",
            turn_index=3,
            sender="B",
            model_id="model-b",
            content="Not until you swear the witness stays anonymous.",
            thinking="SECRET_MARA_THOUGHT",
            created_at="2026-01-01T00:00:03Z",
        ),
    ]

    # Narrator sees attributed A/B turns as user, its own prior narration as assistant, and zero thinking
    msgs_nar = build_messages_for_speaker(debate=debate, history=history, speaker="narrator")
    assert [m["role"] for m in msgs_nar] == ["system", "user", "assistant", "user"]
    assert "[Detective Vance]: Put the ledger on the table, Mara." in msgs_nar[1]["content"]
    assert msgs_nar[2]["content"] == "Vance leans forward as rain lashes the tavern window."
    assert "[Inspector Mara]: Not until you swear the witness stays anonymous." in msgs_nar[3]["content"]
    assert "SECRET_" not in str(msgs_nar)

    # Debater A sees Narrator message clearly attributed and never sees narrator thinking
    msgs_a = build_messages_for_speaker(debate=debate, history=history, speaker="A")
    assert "[Scene Narrator]: Vance leans forward as rain lashes the tavern window." in str(msgs_a)
    assert "SECRET_" not in str(msgs_a)


def test_characters_receive_narrator_reaction_instructions_and_cue() -> None:
    debate = DebateRecord(
        id="deb-rp-react",
        title="Tavern Explosion",
        scene_prompt="Two rival detectives meet in a rain-soaked tavern.",
        first_speaker="A",
        resolved_first_speaker="A",
        status="running",
        turn_limit=6,
        narrator_enabled=True,
        narrator_config=NarratorConfig(model_id="model-narrator", label="Scene Narrator"),
        narrator_cadence_mode="every_turn",
        narrator_cadence_n=1,
        model_a_config=ModelParticipantConfig(
            model_id="model-a",
            label="Detective Vance",
            style="adversarial",
            conviction="firm",
            side="Demand the ledger.",
            dialogue_only=True,
            no_thoughts_as_actions=True,
            no_self_narration=True,
        ),
        model_b_config=ModelParticipantConfig(
            model_id="model-b",
            label="Inspector Mara",
            style="explorative",
            conviction="balanced",
            side="Protect the witness.",
            system_prompt_override="You are Inspector Mara, a wary veteran investigator.",
            dialogue_only=True,
        ),
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )

    # Auto-composed and override system prompts both explain how to react to [Scene Narrator]
    sys_a = compose_system_prompt(
        participant=debate.model_a_config,
        opponent_label="Inspector Mara",
        scene_prompt=debate.scene_prompt,
        narrator_enabled=True,
        narrator_label="Scene Narrator",
    )
    assert "[Scene Narrator]:" in sys_a
    assert "react" in sys_a.lower()

    sys_b = compose_system_prompt(
        participant=debate.model_b_config,
        opponent_label="Detective Vance",
        scene_prompt=debate.scene_prompt,
        narrator_enabled=True,
        narrator_label="Scene Narrator",
    )
    assert "You are Inspector Mara" in sys_b
    assert "[Scene Narrator]:" in sys_b
    assert "react" in sys_b.lower()

    history = [
        MessageRecord(
            id="m1",
            debate_id="deb-rp-react",
            turn_index=1,
            sender="A",
            model_id="model-a",
            content="Put the ledger on the table, Mara.",
            created_at="2026-01-01T00:00:01Z",
        ),
        MessageRecord(
            id="m2",
            debate_id="deb-rp-react",
            turn_index=2,
            sender="narrator",
            model_id="model-narrator",
            content="A brick crashes through the tavern window and shatters the lantern.",
            created_at="2026-01-01T00:00:02Z",
        ),
    ]

    # Speaker B sees A's dialogue clearly labeled separately from Scene Narrator + a reaction cue
    msgs_b = build_messages_for_speaker(debate=debate, history=history, speaker="B")
    assert [m["role"] for m in msgs_b] == ["system", "user"]
    assert "[Detective Vance]: Put the ledger on the table, Mara." in msgs_b[1]["content"]
    assert "[Scene Narrator]: A brick crashes through the tavern window" in msgs_b[1]["content"]
    assert "Inspector Mara" in msgs_b[1]["content"]
    assert "react" in msgs_b[1]["content"].lower()

    # After B replies and narrator acts again, A's own assistant turn stays unprefixed while B & Narrator are attributed
    history.extend(
        [
            MessageRecord(
                id="m3",
                debate_id="deb-rp-react",
                turn_index=3,
                sender="B",
                model_id="model-b",
                content="Get down, Vance! Someone followed us!",
                created_at="2026-01-01T00:00:03Z",
            ),
            MessageRecord(
                id="m4",
                debate_id="deb-rp-react",
                turn_index=4,
                sender="narrator",
                model_id="model-narrator",
                content="Footsteps sprint away down the flooded alley outside.",
                created_at="2026-01-01T00:00:04Z",
            ),
        ]
    )
    msgs_a = build_messages_for_speaker(debate=debate, history=history, speaker="A")
    assert [m["role"] for m in msgs_a] == ["system", "user", "assistant", "user"]
    assert msgs_a[2]["content"] == "Put the ledger on the table, Mara."
    assert "[Inspector Mara]: Get down, Vance! Someone followed us!" in msgs_a[3]["content"]
    assert "[Scene Narrator]: Footsteps sprint away down the flooded alley outside." in msgs_a[3]["content"]
    assert "Detective Vance" in msgs_a[3]["content"]
    assert "react" in msgs_a[3]["content"].lower()
    # Synthetic opening prompt for dialogue_only stays in direct-exchange framing
    assert "directly to 'Inspector Mara'" in msgs_a[1]["content"]

