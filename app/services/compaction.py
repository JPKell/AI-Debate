"""
Purpose: Context window tracker and rolling summary compaction service for long-running two-model debates.
Role: service
Patterns: service-layer, structured-errors
Depends on: app.errors, app.repositories.debates_repo, app.schemas, app.services.llm_client
Exposes: estimate_tokens, CompactionService
Tests: tests/test_compaction.py
Invariants: Never deletes original transcript messages; always disables thinking on summarization calls; prefers larger non-adaptor model for summarization.
"""

from __future__ import annotations

from app.errors import AppError
from app.repositories.debates_repo import DebatesRepository
from app.schemas import (
    CompactionRecord,
    CompactionTrigger,
    DebateRecord,
    MessageRecord,
    ResolvedSpeaker,
)
from app.services.llm_client import OpenWeightGymClient


def estimate_tokens(text: str) -> int:
    """Estimate token count using ~3 UTF-8 bytes per token (matching openWeightGym's admission heuristic)."""
    if not text:
        return 0
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


class CompactionService:
    """Manages context threshold checks and rolling transcript summarization.

    Parameters:
        repo: DebatesRepository for reading history and persisting CompactionRecord rows.
        llm_client: OpenWeightGymClient used to invoke the summarizer model.
    Returns:
        CompactionService instance.
    Side effects:
        Makes non-streaming summarization HTTP requests and writes rows to `compactions`.
    """

    def __init__(self, repo: DebatesRepository, llm_client: OpenWeightGymClient) -> None:
        self._repo = repo
        self._llm_client = llm_client

    def effective_window_for_speaker(
        self,
        debate: DebateRecord,
        speaker: ResolvedSpeaker,
    ) -> int | None:
        """Return active context window (`loaded_context`, falling back to `guaranteed`) for a speaker."""
        if speaker == "A":
            return debate.model_a_ctx_loaded or debate.model_a_ctx_guaranteed
        return debate.model_b_ctx_loaded or debate.model_b_ctx_guaranteed

    def should_auto_compact(
        self,
        debate: DebateRecord,
        history: list[MessageRecord],
        latest_compaction: CompactionRecord | None,
        speaker: ResolvedSpeaker,
    ) -> bool:
        """Determine whether the upcoming turn exceeds `auto_compact_threshold` of the speaker's window.

        Parameters:
            debate: Current DebateRecord with per-model context bounds and threshold.
            history: Full ordered transcript messages.
            latest_compaction: Most recent compaction, if any.
            speaker: Next speaker ('A' or 'B').
        Returns:
            True if compaction should run before invoking `speaker`.
        Side effects:
            None.
        """
        window = self.effective_window_for_speaker(debate, speaker)
        if not window or window <= 0:
            return False

        # Must have enough uncompacted messages beyond keep_recent_turns to actually save context.
        uncompacted = self._messages_after_compaction(history, latest_compaction)
        if len(uncompacted) <= debate.keep_recent_turns:
            return False

        participant = debate.model_a_config if speaker == "A" else debate.model_b_config
        last_speaker_idx = None
        last_prompt_tokens = 0
        for idx in range(len(uncompacted) - 1, -1, -1):
            msg = uncompacted[idx]
            if msg.sender == speaker and msg.prompt_tokens:
                last_speaker_idx = idx
                last_prompt_tokens = msg.prompt_tokens
                break

        if last_speaker_idx is not None:
            new_msgs = uncompacted[last_speaker_idx:]
            added_tokens = sum(estimate_tokens(m.content) + 12 for m in new_msgs)
            estimated_total = last_prompt_tokens + added_tokens + participant.max_tokens
        else:
            base_text = debate.scene_prompt + (
                latest_compaction.summary_text if latest_compaction else ""
            )
            msg_tokens = sum(estimate_tokens(m.content) + 12 for m in uncompacted)
            estimated_total = estimate_tokens(base_text) + msg_tokens + participant.max_tokens + 200

        return estimated_total >= int(window * debate.auto_compact_threshold)

    def select_summarizer_model(self, debate: DebateRecord) -> str:
        """Pick the active debating model with the larger context window, preferring non-adaptor models."""
        candidates: list[tuple[int, int, str]] = []
        for _slot, cfg, loaded, guaranteed in (
            ("A", debate.model_a_config, debate.model_a_ctx_loaded, debate.model_a_ctx_guaranteed),
            ("B", debate.model_b_config, debate.model_b_ctx_loaded, debate.model_b_ctx_guaranteed),
        ):
            model_id = cfg.model_id.strip()
            if not model_id:
                continue
            cached = self._llm_client.get_cached_model(model_id)
            non_adaptor_rank = 0 if (cached and cached.is_adaptor) else 1
            window_rank = (
                loaded
                or guaranteed
                or (cached.loaded_context if cached else None)
                or (cached.context_guaranteed if cached else None)
                or 0
            )
            candidates.append((non_adaptor_rank, window_rank, model_id))

        if not candidates:
            raise AppError(
                code="no_summarizer_model",
                message="Cannot compact debate because no model is configured.",
                status_code=400,
            )
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    async def compact_debate(
        self,
        debate_id: str,
        *,
        trigger_reason: CompactionTrigger = "manual",
        keep_recent_override: int | None = None,
    ) -> CompactionRecord | None:
        """Summarize older debate turns into a rolling neutral digest and persist a CompactionRecord.

        Parameters:
            debate_id: Target debate ID.
            trigger_reason: Why compaction was triggered (`auto`, `manual`, or `context_too_small`).
            keep_recent_override: Optional override for how many recent messages to preserve verbatim.
        Returns:
            Newly persisted CompactionRecord, or None if there are not enough older messages to compact.
        Side effects:
            Calls openWeightGym `/v1/chat/completions` (non-streaming, thinking disabled) and inserts into `compactions`.
        """
        debate = self._repo.require_debate(debate_id)
        history = self._repo.list_messages(debate_id)
        latest = self._repo.get_latest_compaction(debate_id)

        uncompacted = self._messages_after_compaction(history, latest)
        keep_recent = (
            keep_recent_override if keep_recent_override is not None else debate.keep_recent_turns
        )
        # For reactive context_too_small, fall back to keeping 2 or 1 message if needed.
        if len(uncompacted) <= keep_recent and trigger_reason == "context_too_small":
            keep_recent = max(1, min(2, len(uncompacted) - 1))

        if len(uncompacted) <= keep_recent:
            return None

        to_compact = uncompacted[: len(uncompacted) - keep_recent]
        if not to_compact:
            return None

        through_msg = to_compact[-1]
        summarizer_model = self.select_summarizer_model(debate)

        label_a = debate.model_a_config.effective_label("Model A")
        label_b = debate.model_b_config.effective_label("Model B")

        transcript_lines: list[str] = []
        if latest is not None:
            transcript_lines.append(f"[Previous Summary]\n{latest.summary_text.strip()}")
        for msg in to_compact:
            if msg.sender == "A":
                who = f"{label_a} (Model A)"
            elif msg.sender == "B":
                who = f"{label_b} (Model B)"
            else:
                who = "Moderator"
            transcript_lines.append(f"Turn {msg.turn_index} — {who}:\n{msg.content.strip()}")

        prompt_messages = [
            {
                "role": "system",
                "content": (
                    "You are a neutral debate scribe. Summarize the debate turns into a concise, "
                    "information-dense digest (max ~350 words) preserving:\n"
                    "1. Each debater's core thesis and key supporting arguments.\n"
                    "2. Specific concessions, rebuttals, or moderator directions.\n"
                    "3. Remaining open points of disagreement.\n"
                    "Write neutrally with clear attribution. Do not invent claims."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Debate Topic / Scene:\n{debate.scene_prompt.strip() or debate.title}\n\n"
                    "Material to compact:\n\n" + "\n\n".join(transcript_lines)
                ),
            },
        ]

        summary_text = await self._llm_client.complete_summary(
            model_id=summarizer_model,
            messages=prompt_messages,
            max_tokens=700,
            trace_id=f"debate-{debate_id}",
        )
        if not summary_text:
            raise AppError(
                code="compaction_empty",
                message="Summarizer returned an empty compaction summary.",
                status_code=502,
            )

        return self._repo.add_compaction(
            debate_id,
            through_message_id=through_msg.id,
            summary_text=summary_text,
            summarizer_model_id=summarizer_model,
            trigger_reason=trigger_reason,
        )

    @staticmethod
    def _messages_after_compaction(
        history: list[MessageRecord],
        latest_compaction: CompactionRecord | None,
    ) -> list[MessageRecord]:
        if latest_compaction is None:
            return history
        cut_idx = next(
            (i for i, m in enumerate(history) if m.id == latest_compaction.through_message_id),
            None,
        )
        if cut_idx is None:
            return history
        return history[cut_idx + 1 :]
