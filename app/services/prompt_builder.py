"""
Purpose: Compose debate system prompts from Style, Conviction, Side, and Scene, and build role-flipped chat histories.
Role: util
Patterns: none
Depends on: app.schemas
Exposes: STYLE_PRESETS, CONVICTION_LEVELS, compose_system_prompt, build_messages_for_speaker
Tests: tests/test_prompt_builder.py
Invariants: Never includes any model's private thinking text in the conversation history sent to either model.
"""

from __future__ import annotations

from app.schemas import (
    CompactionRecord,
    DebateRecord,
    MessageRecord,
    ModelParticipantConfig,
    ResolvedSpeaker,
)

STYLE_PRESETS: dict[str, dict[str, str]] = {
    "adversarial": {
        "name": "Adversarial",
        "description": "Sharp, incisive debate style challenging weak premises and defending your thesis.",
        "instruction": (
            "Engage in a rigorous, pointed debate. Directly test your opponent's assumptions, "
            "expose logical gaps or counterexamples, and defend your position with clear, compelling arguments."
        ),
    },
    "cooperative": {
        "name": "Cooperative",
        "description": "Constructive dialogue acknowledging strong points while advancing your perspective.",
        "instruction": (
            "Engage constructively and respectfully. Acknowledge valid insights from your opponent, "
            "clarify points of agreement, and thoughtfully explain why your perspective still carries weight."
        ),
    },
    "explorative": {
        "name": "Explorative",
        "description": "Curious, philosophical inquiry probing edge cases, trade-offs, and deeper implications.",
        "instruction": (
            "Explore the topic deeply with intellectual curiosity. Probe edge cases, hidden trade-offs, "
            "and second-order effects while representing your assigned perspective."
        ),
    },
    "custom": {
        "name": "Custom",
        "description": "Use your own custom style instructions.",
        "instruction": "",
    },
}

CONVICTION_LEVELS: dict[str, dict[str, str]] = {
    "immovable": {
        "name": "Immovable",
        "description": "Never change position; treat your stance as non-negotiable.",
        "instruction": (
            "Conviction level — Immovable: Never abandon or soften your core position under any "
            "circumstances. Reframe every objection to reinforce your stance."
        ),
    },
    "firm": {
        "name": "Firm",
        "description": "High evidence bar; hold ground unless given decisive proof.",
        "instruction": (
            "Conviction level — Firm: Hold your position strongly. Require rigorous, decisive evidence "
            "before making even minor concessions."
        ),
    },
    "balanced": {
        "name": "Balanced",
        "description": "Open to strong arguments while steadily advocating your side.",
        "instruction": (
            "Conviction level — Balanced: Advocate clearly for your stance, but acknowledge strong "
            "counter-arguments honestly and refine your reasoning when warranted."
        ),
    },
    "flexible": {
        "name": "Flexible",
        "description": "Willing to shift perspective if persuaded by better reasoning.",
        "instruction": (
            "Conviction level — Flexible: Start from your assigned stance, but remain genuinely open "
            "to shifting your view if your opponent presents stronger reasoning."
        ),
    },
    "synthesis": {
        "name": "Synthesis-seeking",
        "description": "Aim to integrate the strongest parts of both positions into a unified conclusion.",
        "instruction": (
            "Conviction level — Synthesis-seeking: Represent your side clearly while actively searching "
            "for higher-order synthesis that integrates the best ideas from both perspectives."
        ),
    },
}


def compose_system_prompt(
    *,
    participant: ModelParticipantConfig,
    opponent_label: str,
    scene_prompt: str,
) -> str:
    """Compose the full system prompt for one debating model.

    Parameters:
        participant: Configuration for the speaking participant.
        opponent_label: Display label of the opposing participant.
        scene_prompt: Shared topic / framing for the debate.
    Returns:
        System prompt string ready for the LLM `system` message.
    Side effects:
        None.
    """
    if participant.system_prompt_override.strip():
        base = participant.system_prompt_override.strip()
        if scene_prompt.strip() and scene_prompt.strip() not in base:
            return f"{base}\n\nDebate Scene / Topic:\n{scene_prompt.strip()}"
        return base

    my_label = participant.effective_label("Debater")
    style_text = (
        participant.custom_prompt.strip()
        if participant.style == "custom"
        else STYLE_PRESETS.get(participant.style, STYLE_PRESETS["adversarial"])["instruction"]
    )
    conviction_text = CONVICTION_LEVELS.get(
        participant.conviction, CONVICTION_LEVELS["firm"]
    )["instruction"]

    parts: list[str] = [
        f"You are '{my_label}', participating in a live back-and-forth debate against '{opponent_label}'.",
        "Respond directly to your opponent in clear, substantive, well-structured prose. "
        "Keep each turn focused and engaging (avoid repeating earlier points verbatim).",
    ]
    if style_text:
        parts.append(f"Debate Style:\n{style_text}")
    parts.append(conviction_text)
    if participant.side.strip():
        parts.append(f"Your Assigned Side / Position:\n{participant.side.strip()}")
    if scene_prompt.strip():
        parts.append(f"Debate Scene / Topic:\n{scene_prompt.strip()}")
    return "\n\n".join(parts)


def build_messages_for_speaker(
    *,
    debate: DebateRecord,
    history: list[MessageRecord],
    speaker: ResolvedSpeaker,
    latest_compaction: CompactionRecord | None = None,
) -> list[dict[str, str]]:
    """Build OpenAI chat `messages` list for the upcoming speaker with strict role-flipping.

    Parameters:
        debate: Debate record containing both participant configs and scene prompt.
        history: Ordered list of prior transcript messages.
        speaker: Which participant ('A' or 'B') is about to speak.
        latest_compaction: Optional latest compaction summary; when set, only messages after
            `through_message_id` are sent verbatim while earlier turns are represented by the summary.
    Returns:
        List of OpenAI-compatible message dicts (`role`, `content`), guaranteed to exclude thinking text.
    Side effects:
        None.
    """
    participant = debate.model_a_config if speaker == "A" else debate.model_b_config
    opponent = debate.model_b_config if speaker == "A" else debate.model_a_config
    opponent_label = opponent.effective_label("Model B" if speaker == "A" else "Model A")

    system_prompt = compose_system_prompt(
        participant=participant,
        opponent_label=opponent_label,
        scene_prompt=debate.scene_prompt,
    )

    effective_history = history
    if latest_compaction is not None:
        system_prompt = (
            f"{system_prompt}\n\n"
            f"Summary of Earlier Debate Turns (compacted):\n{latest_compaction.summary_text.strip()}"
        )
        cut_idx = next(
            (i for i, m in enumerate(history) if m.id == latest_compaction.through_message_id),
            None,
        )
        if cut_idx is not None:
            effective_history = history[cut_idx + 1 :]

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

    if not effective_history:
        topic_hint = (
            f" on the topic: {debate.scene_prompt.strip()}" if debate.scene_prompt.strip() else ""
        )
        opening = (
            "Continue the debate from the summary above, presenting your next argument clearly."
            if latest_compaction is not None
            else f"The debate is now beginning{topic_hint}. Please deliver your opening statement "
            "clearly establishing your position."
        )
        messages.append({"role": "user", "content": opening})
        return messages

    # Convert history strictly from the speaking model's perspective, excluding all thinking text.
    for msg in effective_history:
        text = msg.content.strip()
        if not text:
            continue
        if msg.sender == speaker:
            role = "assistant"
            content = text
        elif msg.sender == "moderator":
            role = "user"
            content = f"[Moderator]: {text}"
        else:
            role = "user"
            content = text

        # Many llama.cpp / Jinja chat templates require the first non-system message to be 'user'.
        if len(messages) == 1 and role == "assistant":
            topic_hint = (
                f" on the topic: {debate.scene_prompt.strip()}" if debate.scene_prompt.strip() else ""
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The debate is now beginning{topic_hint}. Please deliver your opening statement "
                        "clearly establishing your position."
                    ),
                }
            )

        # Coalesce consecutive same-role messages so strict alternating templates never reject input.
        if messages[-1]["role"] == role and role in ("user", "assistant"):
            messages[-1]["content"] = f"{messages[-1]['content']}\n\n{content}"
        else:
            messages.append({"role": role, "content": content})

    # Ensure the final message prompts a response (role == "user").
    if messages[-1]["role"] != "user":
        messages.append(
            {
                "role": "user",
                "content": "Please continue your argument and respond to the debate so far.",
            }
        )

    return messages
