/**
 * Purpose: EventSource client managing live debate SSE subscriptions and automatic reconnection.
 * Role: component
 * Patterns: streaming-pipeline
 * Depends on: none
 * Exposes: DebateStreamClient
 * Tests: none
 * Invariants: Ensures at most one EventSource connection is open per client instance and cleans up timers on close.
 */

const EVENT_TYPES = [
  "snapshot",
  "status",
  "turn_start",
  "turn_meta",
  "thinking_delta",
  "content_delta",
  "turn_complete",
  "message",
  "compaction",
  "compaction_queued",
  "interjection_queued",
  "error",
  "ping",
];

export class DebateStreamClient {
  constructor(handlers = {}) {
    this.handlers = handlers;
    this.source = null;
    this.debateId = null;
    this.reconnectTimer = null;
  }

  connect(debateId) {
    this.disconnect();
    if (!debateId) return;
    this.debateId = debateId;
    this._open();
  }

  _open() {
    if (!this.debateId) return;
    const url = `/api/debates/${encodeURIComponent(this.debateId)}/stream`;
    const es = new EventSource(url);
    this.source = es;

    for (const type of EVENT_TYPES) {
      es.addEventListener(type, (ev) => {
        if (type === "ping") return;
        let parsed = null;
        try {
          parsed = JSON.parse(ev.data);
        } catch {
          return;
        }
        const fn = this.handlers[type];
        if (typeof fn === "function") {
          fn(parsed);
        }
      });
    }

    es.onerror = () => {
      if (this.source !== es) return;
      es.close();
      this.source = null;
      if (this.debateId && !this.reconnectTimer) {
        this.reconnectTimer = window.setTimeout(() => {
          this.reconnectTimer = null;
          this._open();
        }, 2000);
      }
    };
  }

  disconnect() {
    this.debateId = null;
    if (this.reconnectTimer) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.source) {
      this.source.close();
      this.source = null;
    }
  }
}
