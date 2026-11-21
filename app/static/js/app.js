/**
 * Purpose: Top-level debate workspace controller coordinating model panels, SSE streaming, compaction dividers, custom prompts, and user preferences.
 * Role: page
 * Patterns: streaming-pipeline, structured-errors
 * Depends on: api.js, markdown.js, sse.js
 * Exposes: none
 * Tests: none
 * Invariants: Never hard-codes model IDs; always populates model dropdowns from GET /api/models; redirects to /login on 401.
 */

import { apiRequest } from "./api.js";
import { escapeHtml, renderMarkdown } from "./markdown.js";
import { DebateStreamClient } from "./sse.js";

const state = {
  user: null,
  models: [],
  presets: { styles: {}, convictions: {} },
  customPrompts: [],
  debates: [],
  historyQuery: "",
  globalActiveDebateId: null,
  currentDebate: null,
  messages: [],
  compactions: [],
  liveTurn: null,
  wakeTimer: null,
  showThinking: true,
  saveTimer: null,
};

const el = (id) => document.getElementById(id);

function clearWakeTimer() {
  if (state.wakeTimer) {
    window.clearTimeout(state.wakeTimer);
    state.wakeTimer = null;
  }
}

function startWakeTimer() {
  clearWakeTimer();
  state.wakeTimer = window.setTimeout(() => {
    state.wakeTimer = null;
    if (
      state.liveTurn &&
      !state.liveTurn.thinking &&
      !state.liveTurn.content &&
      !state.liveTurn.status_hint
    ) {
      state.liveTurn.status_hint = "Waking compute node…";
      renderLiveTurn();
    }
  }, 3000);
}

function handleAuthError(err) {
  if (err && err.status === 401) {
    window.location.href = "/login";
    return true;
  }
  return false;
}

const streamClient = new DebateStreamClient({
  snapshot: (data) => {
    state.globalActiveDebateId = data.global_active_debate_id || null;
    if (data.debate) {
      state.currentDebate = data.debate;
      renderDebateControls();
      renderDebatesList();
    }
    if (data.active_turn) {
      state.liveTurn = {
        ...data.active_turn,
        thinkingCollapsed: Boolean(data.active_turn.content),
        thinkingDoneSec: null,
      };
      if (!state.liveTurn.thinking && !state.liveTurn.content) {
        startWakeTimer();
      }
      renderLiveTurn();
    }
  },
  status: (data) => {
    if (data.debate) {
      state.currentDebate = data.debate;
      if (data.debate.status === "running") {
        state.globalActiveDebateId = data.debate.id;
      } else {
        clearWakeTimer();
        if (state.globalActiveDebateId === data.debate.id) {
          state.globalActiveDebateId = null;
        }
        state.liveTurn = null;
        removeLiveTurnCard();
      }
      renderDebateControls();
      renderContextMeters();
      refreshDebatesList();
      if (data.debate.last_error) {
        showBanner(data.debate.last_error, true);
      }
    }
  },
  turn_start: (data) => {
    const feed = el("chat-feed");
    const stick = isNearBottom(feed);
    state.liveTurn = {
      ...data,
      thinking: data.thinking || "",
      content: data.content || "",
      thinkingCollapsed: false,
      thinkingDoneSec: null,
    };
    startWakeTimer();
    removeLiveTurnCard();
    renderLiveTurn();
    if (stick) scrollFeedToBottom();
  },
  turn_meta: (data) => {
    const meta = data.meta || {};
    if (state.liveTurn && meta.status_hint) {
      state.liveTurn.status_hint = meta.status_hint;
      renderLiveTurn();
    } else if (state.liveTurn && meta.estimated_wait) {
      state.liveTurn.status_hint = `Queued (~${meta.estimated_wait}s wait)…`;
      renderLiveTurn();
    }
    if (meta.ignored_parameters) {
      showBanner(`Model ignored unsupported parameters: ${meta.ignored_parameters}`, false);
    }
    if (meta.allowance_warning) {
      showBanner("Spending allowance warning from openWeightGym (nearing limit).", false);
    }
  },
  thinking_delta: (data) => {
    if (!state.liveTurn) return;
    clearWakeTimer();
    const feed = el("chat-feed");
    const stick = isNearBottom(feed);
    state.liveTurn.thinking += data.text || "";
    state.liveTurn.status_hint = null;
    renderLiveTurn();
    if (stick) scrollFeedToBottom();
  },
  content_delta: (data) => {
    if (!state.liveTurn) return;
    clearWakeTimer();
    const feed = el("chat-feed");
    const stick = isNearBottom(feed);
    const hadContent = state.liveTurn.content.length > 0;
    state.liveTurn.content += data.text || "";
    state.liveTurn.status_hint = null;
    if (!hadContent && state.liveTurn.thinking.length > 0) {
      state.liveTurn.thinkingCollapsed = true;
      state.liveTurn.thinkingDoneSec = Math.max(
        1,
        Math.round((Date.now() - state.liveTurn.started_at_ms) / 1000)
      );
    }
    renderLiveTurn();
    if (stick) scrollFeedToBottom();
  },
  turn_complete: (data) => {
    clearWakeTimer();
    const feed = el("chat-feed");
    const stick = isNearBottom(feed);
    state.liveTurn = null;
    if (data.message) {
      upsertMessage(data.message);
      appendOrReplaceMessageCard(data.message, true);
    } else {
      removeLiveTurnCard();
    }
    if (data.debate) {
      state.currentDebate = data.debate;
      renderDebateControls();
      renderContextMeters();
    }
    if (stick) scrollFeedToBottom();
  },
  message: (data) => {
    if (data.message) {
      const feed = el("chat-feed");
      const stick = isNearBottom(feed);
      upsertMessage(data.message);
      appendOrReplaceMessageCard(data.message, false);
      if (stick) scrollFeedToBottom();
    }
  },
  compaction: (data) => {
    if (data.compaction) {
      const feed = el("chat-feed");
      const stick = isNearBottom(feed);
      if (!state.compactions.some((c) => c.id === data.compaction.id)) {
        state.compactions.push(data.compaction);
        insertCompactionDividerInPlace(data.compaction);
      }
      if (stick) scrollFeedToBottom();
      showBanner(
        `Context compacted (${data.compaction.trigger_reason}) using ${data.compaction.summarizer_model_id}.`,
        false
      );
    }
  },
  compaction_queued: (data) => {
    showBanner(data.message || "Context compaction queued for next turn.", false);
  },
  interjection_queued: (data) => {
    showBanner(`Moderator interjection queued for next turn (${data.queued_count} pending).`, false);
  },
  error: (data) => {
    clearWakeTimer();
    showBanner(data.message || "Upstream model error occurred.", true);
    if (data.refresh_models) {
      refreshModels();
    }
  },
});

function upsertMessage(msg) {
  const idx = state.messages.findIndex((m) => m.id === msg.id);
  if (idx >= 0) {
    state.messages[idx] = msg;
  } else {
    state.messages.push(msg);
  }
}

function showBanner(text, isError = false, action = null) {
  const banner = el("banner");
  const actionWrap = el("banner-action");
  el("banner-text").textContent = text;
  actionWrap.innerHTML = "";
  if (action && action.label && typeof action.onClick === "function") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-sm btn-secondary";
    btn.textContent = action.label;
    btn.addEventListener("click", action.onClick);
    actionWrap.appendChild(btn);
    actionWrap.classList.remove("hidden");
  } else {
    actionWrap.classList.add("hidden");
  }
  banner.classList.remove("hidden", "banner-error");
  if (isError) banner.classList.add("banner-error");
}

function hideBanner() {
  el("banner").classList.add("hidden");
  el("banner-action").innerHTML = "";
  el("banner-action").classList.add("hidden");
}

function isNearBottom(container, threshold = 80) {
  if (!container) return true;
  return container.scrollHeight - container.scrollTop - container.clientHeight <= threshold;
}

function scrollFeedToBottom() {
  const feed = el("chat-feed");
  if (!feed) return;
  feed.scrollTop = feed.scrollHeight;
}

function formatContext(n) {
  if (!n) return "?";
  if (n >= 1024) return `${Math.round(n / 1024)}k`;
  return String(n);
}

function populateModelSelects() {
  for (const slot of ["a", "b"]) {
    const sel = el(`${slot}-model`);
    const prev = sel.value;
    sel.innerHTML = "";
    if (state.models.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No chat models available";
      sel.appendChild(opt);
      continue;
    }
    for (const m of state.models) {
      const opt = document.createElement("option");
      opt.value = m.id;
      const parts = [m.id];
      if (m.context_guaranteed && m.context_max) {
        parts.push(`(${formatContext(m.context_guaranteed)}–${formatContext(m.context_max)})`);
      } else if (m.loaded_context) {
        parts.push(`(${formatContext(m.loaded_context)})`);
      }
      if (m.node_may_wake) parts.push("[Node may wake]");
      if (m.is_adaptor) parts.push("[Agent]");
      opt.textContent = parts.join(" ");
      sel.appendChild(opt);
    }
    if (prev && state.models.some((m) => m.id === prev)) {
      sel.value = prev;
    }
  }
  updateModelMetadataUI("a");
  updateModelMetadataUI("b");
}

function populatePresetSelects() {
  for (const slot of ["a", "b"]) {
    const styleSel = el(`${slot}-style`);
    styleSel.innerHTML = "";
    for (const [key, item] of Object.entries(state.presets.styles || {})) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = item.name;
      styleSel.appendChild(opt);
    }

    const convSel = el(`${slot}-conviction`);
    convSel.innerHTML = "";
    for (const [key, item] of Object.entries(state.presets.convictions || {})) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = item.name;
      convSel.appendChild(opt);
    }
  }
}

function populateCustomPromptSelects() {
  for (const slot of ["a", "b"]) {
    const sel = el(`${slot}-saved-prompt`);
    if (!sel) continue;
    const prev = sel.value;
    sel.innerHTML = `<option value="">— Saved custom prompts (${state.customPrompts.length}) —</option>`;
    for (const p of state.customPrompts) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      sel.appendChild(opt);
    }
    if (prev && state.customPrompts.some((p) => p.id === prev)) {
      sel.value = prev;
    }
  }
}

function updateModelMetadataUI(slot) {
  const modelId = el(`${slot}-model`).value;
  const info = state.models.find((m) => m.id === modelId);
  const metaEl = el(`${slot}-model-meta`);
  const effortWrap = el(`${slot}-effort-wrap`);
  const effortSel = el(`${slot}-effort`);

  if (!info) {
    metaEl.textContent = "";
    effortWrap.classList.add("hidden");
    return;
  }

  const bits = [];
  if (info.machines && info.machines.length) bits.push(`Node: ${info.machines.join(", ")}`);
  if (info.loaded_context) bits.push(`Loaded: ${info.loaded_context.toLocaleString()} tok`);
  else if (info.context_guaranteed) bits.push(`Guaranteed: ${info.context_guaranteed.toLocaleString()} tok`);
  if (!info.supports_streaming) bits.push("Non-streaming");
  if (info.node_may_wake) bits.push("Node may wake");
  metaEl.textContent = bits.join(" · ");

  if (info.supports_reasoning_effort && info.reasoning_effort_levels.length > 0) {
    effortWrap.classList.remove("hidden");
    const prev = effortSel.value;
    effortSel.innerHTML = `<option value="">Default</option>`;
    for (const lvl of info.reasoning_effort_levels) {
      const opt = document.createElement("option");
      opt.value = lvl;
      opt.textContent = lvl;
      effortSel.appendChild(opt);
    }
    if (prev) effortSel.value = prev;
  } else {
    effortWrap.classList.add("hidden");
    effortSel.innerHTML = "";
  }
  updateTokenWarning(slot);
}

function updateTokenWarning(slot) {
  const maxTok = Number(el(`${slot}-max-tokens`).value || 0);
  const mode = el(`${slot}-thinking`).value;
  const warn = el(`${slot}-tokens-warn`);
  warn.classList.toggle("hidden", !(maxTok > 0 && maxTok < 512 && mode !== "off"));
}

function readParticipantFromUI(slot) {
  const effortEl = el(`${slot}-effort`);
  const seedRaw = el(`${slot}-seed`) ? el(`${slot}-seed`).value.trim() : "";
  const seedVal = seedRaw !== "" && Number.isFinite(Number(seedRaw)) ? Math.trunc(Number(seedRaw)) : null;
  return {
    model_id: el(`${slot}-model`).value || "",
    label: el(`${slot}-label`).value.trim(),
    style: el(`${slot}-style`).value || "adversarial",
    custom_prompt: el(`${slot}-custom-prompt`).value || "",
    conviction: el(`${slot}-conviction`).value || "firm",
    side: el(`${slot}-side`).value || "",
    system_prompt_override: el(`${slot}-prompt-override`) ? el(`${slot}-prompt-override`).value || "" : "",
    temperature: Number(el(`${slot}-temp`).value || 0.7),
    top_p: Number(el(`${slot}-top-p`).value || 0.95),
    max_tokens: Number(el(`${slot}-max-tokens`).value || 2048),
    seed: seedVal,
    thinking_mode: el(`${slot}-thinking`).value || "default",
    reasoning_effort: effortEl && effortEl.value ? effortEl.value : null,
  };
}

function applyParticipantToUI(slot, cfg) {
  if (!cfg) return;
  const modelSel = el(`${slot}-model`);
  if (cfg.model_id && !state.models.some((m) => m.id === cfg.model_id)) {
    const opt = document.createElement("option");
    opt.value = cfg.model_id;
    opt.textContent = `${cfg.model_id} (offline)`;
    modelSel.appendChild(opt);
  }
  if (cfg.model_id) modelSel.value = cfg.model_id;
  el(`${slot}-label`).value = cfg.label || "";
  el(`${slot}-style`).value = cfg.style || "adversarial";
  el(`${slot}-custom-prompt`).value = cfg.custom_prompt || "";
  el(`${slot}-custom-wrap`).classList.toggle("hidden", cfg.style !== "custom");
  el(`${slot}-conviction`).value = cfg.conviction || "firm";
  el(`${slot}-side`).value = cfg.side || "";
  if (el(`${slot}-prompt-override`)) {
    el(`${slot}-prompt-override`).value = cfg.system_prompt_override || "";
  }
  el(`${slot}-temp`).value = cfg.temperature ?? 0.7;
  el(`${slot}-top-p`).value = cfg.top_p ?? 0.95;
  el(`${slot}-max-tokens`).value = cfg.max_tokens ?? 2048;
  if (el(`${slot}-seed`)) {
    el(`${slot}-seed`).value = cfg.seed ?? "";
  }
  el(`${slot}-thinking`).value = cfg.thinking_mode || "default";
  updateModelMetadataUI(slot);
  if (cfg.reasoning_effort && el(`${slot}-effort`)) {
    el(`${slot}-effort`).value = cfg.reasoning_effort;
  }
}

function composeSystemPromptPreview(slot) {
  const other = slot === "a" ? "b" : "a";
  const me = readParticipantFromUI(slot);
  const opp = readParticipantFromUI(other);
  const scene = (el("scene-prompt")?.value || "").trim();

  if (me.system_prompt_override.trim()) {
    const base = me.system_prompt_override.trim();
    if (scene && !base.includes(scene)) {
      return `${base}\n\nDebate Scene / Topic:\n${scene}`;
    }
    return base;
  }

  const myLabel = me.label || me.model_id || (slot === "a" ? "Model A" : "Model B");
  const oppLabel = opp.label || opp.model_id || (other === "a" ? "Model A" : "Model B");
  const styles = state.presets.styles || {};
  const convictions = state.presets.convictions || {};
  const styleText =
    me.style === "custom"
      ? me.custom_prompt.trim()
      : (styles[me.style]?.instruction || styles.adversarial?.instruction || "");
  const convText =
    convictions[me.conviction]?.instruction || convictions.firm?.instruction || "";

  const parts = [
    `You are '${myLabel}', participating in a live back-and-forth debate against '${oppLabel}'.`,
    "Respond directly to your opponent in clear, substantive, well-structured prose. Keep each turn focused and engaging (avoid repeating earlier points verbatim).",
  ];
  if (styleText) parts.push(`Debate Style:\n${styleText}`);
  if (convText) parts.push(convText);
  if (me.side.trim()) parts.push(`Your Assigned Side / Position:\n${me.side.trim()}`);
  if (scene) parts.push(`Debate Scene / Topic:\n${scene}`);
  return parts.join("\n\n");
}

function updatePromptPreviews() {
  for (const slot of ["a", "b"]) {
    const pre = el(`${slot}-prompt-preview`);
    if (pre) {
      pre.textContent = composeSystemPromptPreview(slot);
    }
  }
}

function readDebatePayloadFromUI() {
  const turnRaw = el("turn-limit").value;
  const minRaw = el("time-limit-min").value;
  return {
    title: el("debate-title").value.trim() || "Untitled Debate",
    scene_prompt: el("scene-prompt").value || "",
    first_speaker: el("first-speaker").value || "A",
    turn_limit: turnRaw ? Number(turnRaw) : null,
    time_limit_seconds: minRaw ? Math.round(Number(minRaw) * 60) : null,
    show_thinking: el("toggle-thinking").checked,
    model_a_config: readParticipantFromUI("a"),
    model_b_config: readParticipantFromUI("b"),
  };
}

function applyDebateToUI(debate) {
  el("debate-title").value = debate.title || "Untitled Debate";
  el("scene-prompt").value = debate.scene_prompt || "";
  el("first-speaker").value = debate.first_speaker || "A";
  el("turn-limit").value = debate.turn_limit ?? "";
  el("time-limit-min").value = debate.time_limit_seconds
    ? Math.max(1, Math.round(debate.time_limit_seconds / 60))
    : "";
  state.showThinking = Boolean(debate.show_thinking);
  el("toggle-thinking").checked = state.showThinking;
  applyParticipantToUI("a", debate.model_a_config);
  applyParticipantToUI("b", debate.model_b_config);
  updatePromptPreviews();
  renderDebateControls();
  renderContextMeters();
}

function renderDebateControls() {
  const d = state.currentDebate;
  const status = d ? d.status : "draft";
  const badge = el("status-badge");
  badge.textContent = status.replace("_", " ");
  badge.className = `status-badge status-${status}`;

  const isRunning = status === "running";
  const isPaused = status === "paused" || status === "error" || status === "stopped";
  const isLimit = status === "limit_reached";

  el("btn-start").classList.toggle("hidden", isRunning || isPaused || isLimit);
  el("btn-pause").classList.toggle("hidden", !isRunning);
  el("btn-resume").classList.toggle("hidden", !isPaused);
  el("continue-group").classList.toggle("hidden", !isLimit);
  el("btn-stop").classList.toggle("hidden", !isRunning);
}

function renderContextMeters() {
  const d = state.currentDebate;
  for (const [slot, sender] of [["a", "A"], ["b", "B"]]) {
    const lastMsg = [...state.messages].reverse().find((m) => m.sender === sender);
    const promptTok = lastMsg ? lastMsg.prompt_tokens : null;
    const loaded = d ? (slot === "a" ? d.model_a_ctx_loaded : d.model_b_ctx_loaded) : null;
    const guaranteed = d ? (slot === "a" ? d.model_a_ctx_guaranteed : d.model_b_ctx_guaranteed) : null;
    const maxCtx = d ? (slot === "a" ? d.model_a_ctx_max : d.model_b_ctx_max) : null;
    const effective = loaded || guaranteed || (lastMsg ? lastMsg.loaded_context_tokens : null);

    const meterEl = el(`${slot}-ctx-meter`);
    const textEl = el(`${slot}-ctx-text`);
    const fillEl = el(`${slot}-ctx-fill`);

    const tipBits = [];
    if (loaded) tipBits.push(`Loaded: ${loaded.toLocaleString()}`);
    if (guaranteed && maxCtx) {
      tipBits.push(`Window range: ${guaranteed.toLocaleString()}–${maxCtx.toLocaleString()}`);
    } else if (guaranteed) {
      tipBits.push(`Guaranteed: ${guaranteed.toLocaleString()}`);
    } else if (maxCtx) {
      tipBits.push(`Max: ${maxCtx.toLocaleString()}`);
    }
    if (meterEl) {
      meterEl.title = tipBits.length
        ? tipBits.join(" | ")
        : "Context window will be learned from X-LoadCoach-Context-Tokens on Turn 1";
    }

    if (promptTok && effective) {
      const pct = Math.min(100, Math.round((promptTok / effective) * 100));
      textEl.textContent = `${promptTok.toLocaleString()} / ${effective.toLocaleString()} (${pct}%)`;
      fillEl.style.width = `${pct}%`;
      fillEl.style.background =
        pct >= 80 ? "var(--status-err)" : pct >= 60 ? "var(--status-warn)" : "var(--status-ok)";
    } else if (effective) {
      textEl.textContent = `Window: ${effective.toLocaleString()} tok`;
      fillEl.style.width = "0%";
    } else {
      textEl.textContent = "Learned on Turn 1";
      fillEl.style.width = "0%";
    }
  }
}

function speakerDisplayName(sender) {
  const d = state.currentDebate;
  if (sender === "moderator") return "Moderator";
  const cfg = d ? (sender === "A" ? d.model_a_config : d.model_b_config) : null;
  if (cfg && cfg.label) return cfg.label;
  if (cfg && cfg.model_id) return cfg.model_id;
  return sender === "A" ? "Model A" : "Model B";
}

function buildMessageCardHtml(msg) {
  const rowClass =
    msg.sender === "A" ? "msg-a" : msg.sender === "B" ? "msg-b" : "msg-moderator";
  const senderName = escapeHtml(speakerDisplayName(msg.sender));

  let thinkingHtml = "";
  if (msg.thinking && msg.thinking.trim() && state.showThinking) {
    thinkingHtml = `
      <div class="thinking-box collapsed">
        <button type="button" class="thinking-toggle">
          <span>Thought process</span>
          <span>Show</span>
        </button>
        <div class="thinking-body">${escapeHtml(msg.thinking)}</div>
      </div>
    `;
  }

  let statsHtml = "";
  if (msg.sender === "A" || msg.sender === "B") {
    const bits = [];
    if (msg.answered_model_id || msg.model_id) {
      bits.push(escapeHtml(msg.answered_model_id || msg.model_id));
    }
    if (msg.answered_node) bits.push(`@${escapeHtml(msg.answered_node)}`);
    if (msg.completion_tokens != null) bits.push(`${msg.completion_tokens} tok`);
    if (msg.tokens_per_sec != null) bits.push(`${msg.tokens_per_sec} tok/s`);
    if (msg.duration_ms != null) bits.push(`${(msg.duration_ms / 1000).toFixed(1)}s`);
    const d = state.currentDebate;
    const effCtx =
      msg.loaded_context_tokens ||
      (d
        ? msg.sender === "A"
          ? d.model_a_ctx_loaded || d.model_a_ctx_guaranteed
          : d.model_b_ctx_loaded || d.model_b_ctx_guaranteed
        : null);
    if (msg.prompt_tokens != null && effCtx) {
      const ctxPct = Math.min(100, Math.round((msg.prompt_tokens / effCtx) * 100));
      bits.push(`ctx ${ctxPct}%`);
    }
    if (msg.finish_reason === "length" && !msg.content.trim()) {
      bits.push("⚠ Hit max_tokens during thinking — increase Max Tokens");
    }
    if (bits.length) {
      statsHtml = `<div class="msg-stats">${bits.map((b) => `<span>${b}</span>`).join("")}</div>`;
    }
  }

  const bodyHtml = msg.content.trim()
    ? renderMarkdown(msg.content)
    : `<p><em>(No answer text produced${msg.finish_reason ? ` — ${escapeHtml(msg.finish_reason)}` : ""})</em></p>`;

  return `
    <article class="msg-row ${rowClass}" data-msg-id="${escapeHtml(msg.id)}">
      <div class="msg-card">
        <div class="msg-header">
          <span class="msg-sender">${senderName}</span>
          <span class="msg-turn-num">Turn ${msg.turn_index}</span>
        </div>
        ${thinkingHtml}
        <div class="msg-content">${bodyHtml}</div>
        ${statsHtml}
      </div>
    </article>
  `;
}

function buildCompactionDividerHtml(comp) {
  return `
    <details class="compaction-divider" data-compaction-id="${escapeHtml(comp.id)}">
      <summary>
        <span>🗜 Context Compacted (${escapeHtml(comp.trigger_reason)})</span>
        <span>by ${escapeHtml(comp.summarizer_model_id)} · Show summary</span>
      </summary>
      <div class="compaction-summary-body">${renderMarkdown(comp.summary_text)}</div>
    </details>
  `;
}

function renderTranscript() {
  const feed = el("chat-feed");
  if (state.messages.length === 0 && !state.liveTurn) {
    feed.innerHTML = `
      <div class="empty-state">
        <h2>Ready to Debate</h2>
        <p>Configure Model A and Model B on the side panels, enter a scene above, and press <strong>Start</strong>.</p>
      </div>
    `;
    return;
  }

  const compactionsByMsg = new Map();
  for (const c of state.compactions) {
    const arr = compactionsByMsg.get(c.through_message_id) || [];
    arr.push(c);
    compactionsByMsg.set(c.through_message_id, arr);
  }

  const parts = [];
  for (const msg of state.messages) {
    parts.push(buildMessageCardHtml(msg));
    const attached = compactionsByMsg.get(msg.id);
    if (attached) {
      for (const comp of attached) {
        parts.push(buildCompactionDividerHtml(comp));
      }
    }
  }
  feed.innerHTML = parts.join("");
  if (state.liveTurn) {
    renderLiveTurn();
  }
}

function htmlToElement(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

function appendOrReplaceMessageCard(msg, replaceLiveCard = false) {
  const feed = el("chat-feed");
  const empty = feed.querySelector(".empty-state");
  if (empty) empty.remove();

  const newNode = htmlToElement(buildMessageCardHtml(msg));
  const existing = feed.querySelector(`[data-msg-id="${CSS.escape(msg.id)}"]`);
  const liveCard = document.getElementById("live-turn-card");

  if (existing) {
    existing.replaceWith(newNode);
    if (replaceLiveCard && liveCard) liveCard.remove();
    return;
  }

  if (replaceLiveCard && liveCard) {
    liveCard.replaceWith(newNode);
  } else if (liveCard) {
    feed.insertBefore(newNode, liveCard);
  } else {
    feed.appendChild(newNode);
  }
}

function insertCompactionDividerInPlace(comp) {
  const feed = el("chat-feed");
  if (feed.querySelector(`[data-compaction-id="${CSS.escape(comp.id)}"]`)) return;
  const divider = htmlToElement(buildCompactionDividerHtml(comp));
  const targetMsg = feed.querySelector(`[data-msg-id="${CSS.escape(comp.through_message_id)}"]`);
  if (targetMsg) {
    targetMsg.insertAdjacentElement("afterend", divider);
  } else {
    const liveCard = document.getElementById("live-turn-card");
    if (liveCard) feed.insertBefore(divider, liveCard);
    else feed.appendChild(divider);
  }
}

function removeLiveTurnCard() {
  const existing = document.getElementById("live-turn-card");
  if (existing) existing.remove();
}

function renderLiveTurn() {
  const turn = state.liveTurn;
  if (!turn) return;
  const feed = el("chat-feed");
  const empty = feed.querySelector(".empty-state");
  if (empty) empty.remove();

  const rowClass = turn.speaker === "A" ? "msg-a" : "msg-b";
  let card = document.getElementById("live-turn-card");
  if (!card) {
    card = document.createElement("article");
    card.id = "live-turn-card";
    card.className = `msg-row ${rowClass}`;
    card.innerHTML = `
      <div class="msg-card">
        <div class="msg-header">
          <span class="msg-sender live-sender"></span>
          <span class="msg-turn-num live-turn-num"></span>
        </div>
        <div class="thinking-indicator live-hint hidden"></div>
        <div class="thinking-indicator live-thinking-hidden-ind hidden">Thinking…</div>
        <div class="thinking-box live-thinking-box hidden" data-live-thinking="1">
          <button type="button" class="thinking-toggle">
            <span class="live-thinking-label">Thinking…</span>
            <span class="live-thinking-toggle-text">Hide</span>
          </button>
          <div class="thinking-body live-thinking-body"></div>
        </div>
        <div class="msg-content live-content"></div>
      </div>
    `;
    card.dataset.renderedThinkingLen = "0";
    card.dataset.renderedContentLen = "0";
    feed.appendChild(card);
  } else {
    card.className = `msg-row ${rowClass}`;
  }

  const senderEl = card.querySelector(".live-sender");
  const turnNumEl = card.querySelector(".live-turn-num");
  const hintEl = card.querySelector(".live-hint");
  const hiddenIndEl = card.querySelector(".live-thinking-hidden-ind");
  const thinkBox = card.querySelector(".live-thinking-box");
  const thinkLabelEl = card.querySelector(".live-thinking-label");
  const thinkToggleTextEl = card.querySelector(".live-thinking-toggle-text");
  const thinkBodyEl = card.querySelector(".live-thinking-body");
  const contentEl = card.querySelector(".live-content");

  senderEl.textContent = speakerDisplayName(turn.speaker);
  turnNumEl.textContent = `Turn ${turn.turn_index} · streaming`;

  const hintText = turn.status_hint
    ? turn.status_hint
    : !turn.thinking && !turn.content
      ? `Waiting for ${turn.model_id}…`
      : "";
  hintEl.textContent = hintText;
  hintEl.classList.toggle("hidden", !hintText);

  const showThinkBox = Boolean(turn.thinking && state.showThinking);
  const showHiddenInd = Boolean(turn.thinking && !state.showThinking && !turn.content);
  hiddenIndEl.classList.toggle("hidden", !showHiddenInd);
  thinkBox.classList.toggle("hidden", !showThinkBox);

  if (showThinkBox) {
    thinkBox.classList.toggle("collapsed", Boolean(turn.thinkingCollapsed));
    thinkLabelEl.textContent = turn.thinkingDoneSec
      ? `Thought for ${turn.thinkingDoneSec}s`
      : "Thinking…";
    thinkToggleTextEl.textContent = turn.thinkingCollapsed ? "Show" : "Hide";

    if (Number(card.dataset.renderedThinkingLen) !== turn.thinking.length) {
      const stickThink = isNearBottom(thinkBodyEl, 40);
      thinkBodyEl.textContent = turn.thinking;
      card.dataset.renderedThinkingLen = String(turn.thinking.length);
      if (stickThink && !turn.thinkingCollapsed) {
        thinkBodyEl.scrollTop = thinkBodyEl.scrollHeight;
      }
    }
  }

  if (Number(card.dataset.renderedContentLen) !== turn.content.length) {
    contentEl.innerHTML = turn.content ? renderMarkdown(turn.content) : "";
    card.dataset.renderedContentLen = String(turn.content.length);
  }
}

function renderDebatesList() {
  const list = el("debates-list");
  const q = (state.historyQuery || "").trim().toLowerCase();
  const filtered = q
    ? state.debates.filter(
        (d) =>
          (d.title || "").toLowerCase().includes(q) ||
          (d.status || "").toLowerCase().includes(q)
      )
    : state.debates;

  if (filtered.length === 0) {
    list.innerHTML = `<div class="field-hint" style="padding:8px;">${
      q ? "No matching debates." : "No saved debates yet."
    }</div>`;
    return;
  }
  list.innerHTML = filtered
    .map((d) => {
      const active = state.currentDebate && state.currentDebate.id === d.id ? "active" : "";
      const isRunning = d.status === "running" || state.globalActiveDebateId === d.id;
      const runDot = isRunning ? `<span class="running-dot" title="Currently running"></span>` : "";
      return `
        <div class="debate-item ${active} ${isRunning ? "is-running" : ""}" data-debate-id="${escapeHtml(d.id)}">
          <div class="debate-item-main">
            <div class="debate-item-title">${runDot}${escapeHtml(d.title)}</div>
            <div class="debate-item-sub">${escapeHtml(d.status)} · ${d.message_count} msgs</div>
          </div>
          <div class="debate-item-actions">
            <button class="debate-action-btn" data-rename-id="${escapeHtml(d.id)}" title="Rename">✎</button>
            <button class="debate-action-btn" data-fork-id="${escapeHtml(d.id)}" title="Duplicate">⧉</button>
            <button class="debate-delete-btn" data-delete-id="${escapeHtml(d.id)}" title="Delete">✕</button>
          </div>
        </div>
      `;
    })
    .join("");
}

async function refreshModels() {
  const pill = el("btn-endpoint-status");
  const label = el("endpoint-label");
  pill.classList.remove("ok", "err");
  label.textContent = "Loading models…";
  try {
    const res = await apiRequest("/api/models");
    state.models = res.models || [];
    populateModelSelects();
    pill.classList.add("ok");
    label.textContent = `${state.models.length} chat model${state.models.length === 1 ? "" : "s"}`;
  } catch (err) {
    if (handleAuthError(err)) return;
    pill.classList.add("err");
    label.textContent = "Endpoint error";
    showBanner(`Could not load models from openWeightGym: ${err.message}`, true);
  }
}

async function refreshPrompts() {
  const res = await apiRequest("/api/prompts");
  state.presets = { styles: res.styles || {}, convictions: res.convictions || {} };
  state.customPrompts = res.custom_prompts || [];
  populatePresetSelects();
  populateCustomPromptSelects();
}

async function refreshDebatesList() {
  state.debates = await apiRequest("/api/debates");
  renderDebatesList();
}

async function openDebate(debateId) {
  hideBanner();
  clearWakeTimer();
  const detail = await apiRequest(`/api/debates/${encodeURIComponent(debateId)}`);
  state.currentDebate = detail.debate;
  state.globalActiveDebateId = detail.global_active_debate_id || null;
  state.messages = detail.messages || [];
  state.compactions = detail.compactions || [];
  state.liveTurn = detail.active_turn
    ? {
        ...detail.active_turn,
        thinkingCollapsed: Boolean(detail.active_turn.content),
        thinkingDoneSec: null,
      }
    : null;
  applyDebateToUI(detail.debate);
  renderTranscript();
  scrollFeedToBottom();
  renderDebatesList();
  streamClient.connect(detail.debate.id);
  if (detail.debate.last_error) {
    showBanner(detail.debate.last_error, true);
  }
}

async function createFreshDebate() {
  hideBanner();
  const modelA = state.models[0]?.id || "";
  const modelB = state.models[1]?.id || state.models[0]?.id || "";
  const created = await apiRequest("/api/debates", {
    method: "POST",
    body: {
      title: "New Debate",
      scene_prompt: "",
      first_speaker: "A",
      turn_limit: 6,
      show_thinking: state.user ? state.user.show_thinking_default : true,
      model_a_config: {
        model_id: modelA,
        label: modelA ? `${modelA} (A)` : "Model A",
        style: "adversarial",
        conviction: "firm",
        side: "",
        max_tokens: 2048,
      },
      model_b_config: {
        model_id: modelB,
        label: modelB ? `${modelB} (B)` : "Model B",
        style: "adversarial",
        conviction: "firm",
        side: "",
        max_tokens: 2048,
      },
    },
  });
  await refreshDebatesList();
  await openDebate(created.id);
}

async function saveCurrentDebateNow() {
  if (!state.currentDebate) return;
  const payload = readDebatePayloadFromUI();
  const updated = await apiRequest(`/api/debates/${encodeURIComponent(state.currentDebate.id)}`, {
    method: "PATCH",
    body: payload,
  });
  state.currentDebate = updated;
  renderDebatesList();
}

function scheduleSave() {
  if (!state.currentDebate) return;
  if (state.saveTimer) window.clearTimeout(state.saveTimer);
  state.saveTimer = window.setTimeout(async () => {
    state.saveTimer = null;
    try {
      await saveCurrentDebateNow();
    } catch (err) {
      if (!handleAuthError(err)) showBanner(err.message, true);
    }
  }, 400);
}

function showDebateActionError(err) {
  if (handleAuthError(err)) return;
  if (err && err.status === 409 && err.details && err.details.active_debate_id) {
    const activeId = err.details.active_debate_id;
    state.globalActiveDebateId = activeId;
    renderDebatesList();
    showBanner(err.message, true, {
      label: "Jump to running debate →",
      onClick: () => openDebate(activeId),
    });
    return;
  }
  showBanner(err.message, true);
}

async function handleStartOrResume(action) {
  hideBanner();
  try {
    if (!state.currentDebate) {
      await createFreshDebate();
    }
    await saveCurrentDebateNow();
    const id = state.currentDebate.id;
    const updated = await apiRequest(`/api/debates/${encodeURIComponent(id)}/${action}`, {
      method: "POST",
    });
    state.currentDebate = updated;
    state.globalActiveDebateId = updated.id;
    renderDebateControls();
    refreshDebatesList();
  } catch (err) {
    showDebateActionError(err);
  }
}

async function handleContinue(payload) {
  if (!state.currentDebate) return;
  hideBanner();
  try {
    await saveCurrentDebateNow();
    const updated = await apiRequest(
      `/api/debates/${encodeURIComponent(state.currentDebate.id)}/continue`,
      { method: "POST", body: payload }
    );
    state.currentDebate = updated;
    state.globalActiveDebateId = updated.id;
    renderDebateControls();
    refreshDebatesList();
  } catch (err) {
    showDebateActionError(err);
  }
}

async function handleFork(debateId, swapSides) {
  hideBanner();
  try {
    if (state.currentDebate && state.currentDebate.id === debateId) {
      await saveCurrentDebateNow();
    }
    const forked = await apiRequest(`/api/debates/${encodeURIComponent(debateId)}/fork`, {
      method: "POST",
      body: { swap_sides: swapSides },
    });
    await refreshDebatesList();
    await openDebate(forked.id);
    showBanner(
      swapSides
        ? "Forked debate with Model A & B sides swapped."
        : "Duplicated debate into a new draft.",
      false
    );
  } catch (err) {
    if (!handleAuthError(err)) showBanner(err.message, true);
  }
}

function closePopovers() {
  el("fork-menu")?.classList.add("hidden");
  el("export-menu")?.classList.add("hidden");
}

function bindCustomPromptControls(slot) {
  const selectEl = el(`${slot}-saved-prompt`);
  const textEl = el(`${slot}-custom-prompt`);
  const saveBtn = el(`${slot}-save-prompt`);
  const updateBtn = el(`${slot}-update-prompt`);
  const delBtn = el(`${slot}-del-prompt`);

  selectEl.addEventListener("change", () => {
    const chosen = state.customPrompts.find((p) => p.id === selectEl.value);
    if (chosen) {
      textEl.value = chosen.content;
      updatePromptPreviews();
      scheduleSave();
    }
  });

  saveBtn.addEventListener("click", async () => {
    const content = textEl.value.trim();
    if (!content) {
      showBanner("Write custom style instructions before saving.", true);
      return;
    }
    const name = window.prompt("Name for this reusable custom prompt:");
    if (!name || !name.trim()) return;
    try {
      const created = await apiRequest("/api/prompts", {
        method: "POST",
        body: { name: name.trim(), style_category: "custom", content },
      });
      await refreshPrompts();
      selectEl.value = created.id;
      showBanner(`Saved custom prompt "${created.name}".`, false);
    } catch (err) {
      showBanner(err.message, true);
    }
  });

  if (updateBtn) {
    updateBtn.addEventListener("click", async () => {
      const pid = selectEl.value;
      const content = textEl.value.trim();
      if (!pid) {
        showBanner("Select a saved custom prompt to update.", true);
        return;
      }
      if (!content) {
        showBanner("Custom style instructions cannot be empty.", true);
        return;
      }
      try {
        const updated = await apiRequest(`/api/prompts/${encodeURIComponent(pid)}`, {
          method: "PATCH",
          body: { content },
        });
        await refreshPrompts();
        selectEl.value = updated.id;
        showBanner(`Updated custom prompt "${updated.name}".`, false);
      } catch (err) {
        showBanner(err.message, true);
      }
    });
  }

  delBtn.addEventListener("click", async () => {
    const pid = selectEl.value;
    if (!pid) return;
    try {
      await apiRequest(`/api/prompts/${encodeURIComponent(pid)}`, { method: "DELETE" });
      await refreshPrompts();
      showBanner("Deleted custom prompt.", false);
    } catch (err) {
      showBanner(err.message, true);
    }
  });
}

function bindEvents() {
  el("btn-endpoint-status").addEventListener("click", refreshModels);
  el("btn-dismiss-banner").addEventListener("click", hideBanner);
  el("btn-new-debate").addEventListener("click", createFreshDebate);

  el("debates-search")?.addEventListener("input", (ev) => {
    state.historyQuery = ev.target.value || "";
    renderDebatesList();
  });

  el("btn-logout").addEventListener("click", async () => {
    await apiRequest("/api/auth/logout", { method: "POST" });
    window.location.href = "/login";
  });

  el("theme-select").addEventListener("change", async (ev) => {
    const theme = ev.target.value;
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("ai_debate_theme", theme);
    try {
      await apiRequest("/api/auth/preferences", { method: "PATCH", body: { theme } });
    } catch {
      // Ignore background preference sync error
    }
  });

  el("toggle-thinking").addEventListener("change", async (ev) => {
    state.showThinking = ev.target.checked;
    const expThink = el("export-include-thinking");
    if (expThink) expThink.checked = state.showThinking;
    renderTranscript();
    scheduleSave();
    try {
      await apiRequest("/api/auth/preferences", {
        method: "PATCH",
        body: { show_thinking_default: state.showThinking },
      });
    } catch {
      // Ignore background preference sync error
    }
  });

  bindCustomPromptControls("a");
  bindCustomPromptControls("b");

  // History & mobile drawers
  const closeMobileOverlays = () => {
    el("panel-a").classList.remove("mobile-open");
    el("panel-b").classList.remove("mobile-open");
    el("history-drawer").classList.remove("mobile-open");
    el("mobile-backdrop").classList.add("hidden");
  };

  el("btn-toggle-history").addEventListener("click", () => {
    if (window.innerWidth < 1100) {
      el("history-drawer").classList.toggle("mobile-open");
      el("mobile-backdrop").classList.toggle(
        "hidden",
        !el("history-drawer").classList.contains("mobile-open")
      );
    } else {
      el("history-drawer").classList.toggle("collapsed");
    }
  });

  el("btn-mobile-a").addEventListener("click", () => {
    closeMobileOverlays();
    el("panel-a").classList.add("mobile-open");
    el("mobile-backdrop").classList.remove("hidden");
  });

  el("btn-mobile-b").addEventListener("click", () => {
    closeMobileOverlays();
    el("panel-b").classList.add("mobile-open");
    el("mobile-backdrop").classList.remove("hidden");
  });

  el("mobile-backdrop").addEventListener("click", closeMobileOverlays);
  document.querySelectorAll(".panel-close-btn").forEach((btn) => {
    btn.addEventListener("click", closeMobileOverlays);
  });

  // Close popovers on outside click
  document.addEventListener("click", (ev) => {
    if (!ev.target.closest(".menu-wrap")) {
      closePopovers();
    }
  });

  // Debates list click / rename / fork / delete
  el("debates-list").addEventListener("click", async (ev) => {
    const renameBtn = ev.target.closest("[data-rename-id]");
    if (renameBtn) {
      ev.stopPropagation();
      const id = renameBtn.getAttribute("data-rename-id");
      const existing = state.debates.find((d) => d.id === id);
      const nextTitle = window.prompt("Rename debate:", existing ? existing.title : "");
      if (!nextTitle || !nextTitle.trim()) return;
      try {
        const updated = await apiRequest(`/api/debates/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: { title: nextTitle.trim() },
        });
        if (state.currentDebate && state.currentDebate.id === id) {
          state.currentDebate = updated;
          el("debate-title").value = updated.title;
        }
        await refreshDebatesList();
      } catch (err) {
        if (!handleAuthError(err)) showBanner(err.message, true);
      }
      return;
    }

    const forkBtn = ev.target.closest("[data-fork-id]");
    if (forkBtn) {
      ev.stopPropagation();
      const id = forkBtn.getAttribute("data-fork-id");
      await handleFork(id, false);
      return;
    }

    const delBtn = ev.target.closest("[data-delete-id]");
    if (delBtn) {
      ev.stopPropagation();
      const id = delBtn.getAttribute("data-delete-id");
      await apiRequest(`/api/debates/${encodeURIComponent(id)}`, { method: "DELETE" });
      await refreshDebatesList();
      if (state.currentDebate && state.currentDebate.id === id) {
        if (state.debates.length > 0) {
          await openDebate(state.debates[0].id);
        } else {
          await createFreshDebate();
        }
      }
      return;
    }

    const item = ev.target.closest("[data-debate-id]");
    if (item) {
      closeMobileOverlays();
      await openDebate(item.getAttribute("data-debate-id"));
    }
  });

  // Thinking box toggle delegation
  el("chat-feed").addEventListener("click", (ev) => {
    const toggle = ev.target.closest(".thinking-toggle");
    if (!toggle) return;
    const box = toggle.closest(".thinking-box");
    if (!box) return;
    const nowCollapsed = box.classList.toggle("collapsed");
    const spans = toggle.querySelectorAll("span");
    if (spans.length >= 2) {
      spans[1].textContent = nowCollapsed ? "Show" : "Hide";
    }
    if (box.hasAttribute("data-live-thinking") && state.liveTurn) {
      state.liveTurn.thinkingCollapsed = nowCollapsed;
    }
  });

  // Autosave bindings
  const autosaveIds = [
    "debate-title",
    "scene-prompt",
    "first-speaker",
    "turn-limit",
    "time-limit-min",
    "a-model",
    "a-label",
    "a-style",
    "a-conviction",
    "a-custom-prompt",
    "a-side",
    "a-prompt-override",
    "a-thinking",
    "a-effort",
    "a-temp",
    "a-top-p",
    "a-max-tokens",
    "a-seed",
    "b-model",
    "b-label",
    "b-style",
    "b-conviction",
    "b-custom-prompt",
    "b-side",
    "b-prompt-override",
    "b-thinking",
    "b-effort",
    "b-temp",
    "b-top-p",
    "b-max-tokens",
    "b-seed",
  ];
  for (const id of autosaveIds) {
    const node = el(id);
    if (!node) continue;
    const evt = node.tagName === "SELECT" ? "change" : "input";
    node.addEventListener(evt, () => {
      if (id === "a-model") updateModelMetadataUI("a");
      if (id === "b-model") updateModelMetadataUI("b");
      if (id === "a-style") el("a-custom-wrap").classList.toggle("hidden", node.value !== "custom");
      if (id === "b-style") el("b-custom-wrap").classList.toggle("hidden", node.value !== "custom");
      if (id === "a-max-tokens" || id === "a-thinking") updateTokenWarning("a");
      if (id === "b-max-tokens" || id === "b-thinking") updateTokenWarning("b");
      updatePromptPreviews();
      scheduleSave();
    });
  }

  // Lifecycle buttons
  el("btn-start").addEventListener("click", () => handleStartOrResume("start"));
  el("btn-resume").addEventListener("click", () => handleStartOrResume("resume"));
  el("btn-pause").addEventListener("click", async () => {
    if (!state.currentDebate) return;
    try {
      const updated = await apiRequest(
        `/api/debates/${encodeURIComponent(state.currentDebate.id)}/pause`,
        { method: "POST" }
      );
      state.currentDebate = updated;
      renderDebateControls();
    } catch (err) {
      showBanner(err.message, true);
    }
  });
  el("btn-stop").addEventListener("click", async () => {
    if (!state.currentDebate) return;
    try {
      const updated = await apiRequest(
        `/api/debates/${encodeURIComponent(state.currentDebate.id)}/stop`,
        { method: "POST" }
      );
      state.currentDebate = updated;
      renderDebateControls();
    } catch (err) {
      showBanner(err.message, true);
    }
  });

  // Continue options (+5 turns, +10 turns, +5 min, custom)
  el("btn-continue-5")?.addEventListener("click", () => handleContinue({ add_turns: 5 }));
  el("btn-continue-10")?.addEventListener("click", () => handleContinue({ add_turns: 10 }));
  el("btn-continue-5m")?.addEventListener("click", () => handleContinue({ add_seconds: 300 }));
  el("btn-continue-custom")?.addEventListener("click", async () => {
    const turnsRaw = window.prompt("Additional turns to add (leave blank to skip):", "6");
    if (turnsRaw === null) return;
    const minsRaw = window.prompt("Additional minutes to add (leave blank to skip):", "");
    if (minsRaw === null) return;
    const body = {};
    if (turnsRaw.trim()) {
      const t = Number(turnsRaw.trim());
      if (Number.isInteger(t) && t >= 1 && t <= 100) body.add_turns = t;
    }
    if (minsRaw.trim()) {
      const m = Number(minsRaw.trim());
      if (Number.isFinite(m) && m > 0) body.add_seconds = Math.min(3600, Math.max(10, Math.round(m * 60)));
    }
    if (!body.add_turns && !body.add_seconds) {
      showBanner("Specify a valid number of turns (1–100) or minutes.", true);
      return;
    }
    await handleContinue(body);
  });

  el("btn-compact").addEventListener("click", async () => {
    if (!state.currentDebate) return;
    hideBanner();
    try {
      const res = await apiRequest(
        `/api/debates/${encodeURIComponent(state.currentDebate.id)}/compact`,
        { method: "POST" }
      );
      if (res.queued) {
        showBanner("Context compaction queued for the next turn boundary.", false);
      } else if (res.compaction) {
        if (!state.compactions.some((c) => c.id === res.compaction.id)) {
          state.compactions.push(res.compaction);
          insertCompactionDividerInPlace(res.compaction);
        }
        showBanner("Context compacted successfully.", false);
      }
    } catch (err) {
      showBanner(err.message, true);
    }
  });

  // Fork popover menu (Duplicate vs Fork & Swap Sides)
  el("btn-fork-menu")?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    el("export-menu")?.classList.add("hidden");
    el("fork-menu")?.classList.toggle("hidden");
  });
  el("btn-fork-copy")?.addEventListener("click", async () => {
    closePopovers();
    if (!state.currentDebate) return;
    await handleFork(state.currentDebate.id, false);
  });
  el("btn-fork-swap")?.addEventListener("click", async () => {
    closePopovers();
    if (!state.currentDebate) return;
    await handleFork(state.currentDebate.id, true);
  });

  // Export popover menu (thinking & stats checkboxes + .md / .json)
  el("btn-export-menu")?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    el("fork-menu")?.classList.add("hidden");
    el("export-menu")?.classList.toggle("hidden");
  });
  const triggerExport = (fmt) => {
    closePopovers();
    if (!state.currentDebate) return;
    const includeThinking = el("export-include-thinking")
      ? el("export-include-thinking").checked
      : state.showThinking;
    const includeStats = el("export-include-stats")
      ? el("export-include-stats").checked
      : true;
    const url = `/api/debates/${encodeURIComponent(state.currentDebate.id)}/export?format=${fmt}&include_thinking=${includeThinking}&include_stats=${includeStats}`;
    window.open(url, "_blank");
  };
  el("btn-export-md")?.addEventListener("click", () => triggerExport("md"));
  el("btn-export-json")?.addEventListener("click", () => triggerExport("json"));

  // Moderator interjection
  el("moderator-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!state.currentDebate) return;
    const input = el("moderator-input");
    const text = input.value.trim();
    if (!text) return;
    try {
      await apiRequest(`/api/debates/${encodeURIComponent(state.currentDebate.id)}/interject`, {
        method: "POST",
        body: { content: text },
      });
      input.value = "";
    } catch (err) {
      showBanner(err.message, true);
    }
  });
}

async function init() {
  bindEvents();

  try {
    state.user = await apiRequest("/api/auth/me");
    if (state.user && state.user.theme) {
      document.documentElement.setAttribute("data-theme", state.user.theme);
      localStorage.setItem("ai_debate_theme", state.user.theme);
      el("theme-select").value = state.user.theme;
    }
  } catch (err) {
    if (handleAuthError(err)) return;
  }

  try {
    await refreshPrompts();
  } catch (err) {
    if (!handleAuthError(err)) showBanner(`Failed to load presets: ${err.message}`, true);
  }

  await refreshModels();
  await refreshDebatesList();

  if (state.debates.length > 0) {
    await openDebate(state.debates[0].id);
  } else {
    await createFreshDebate();
  }
}

init();
