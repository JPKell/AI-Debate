"""Observability logger and metrics summarizer for MCP calls and fallback reads/greps."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


def append_event(log_path: Path, event: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": _now().isoformat(), **event}
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_mcp_call(
    log_path: Path,
    tool: str,
    args: dict[str, Any],
    result_bytes: int,
    duration_ms: float,
    ok: bool = True,
    error: str | None = None,
) -> None:
    append_event(
        log_path,
        {
            "event": "mcp_call",
            "tool": tool,
            "args": args,
            "result_bytes": result_bytes,
            "duration_ms": round(duration_ms, 2),
            "ok": ok,
            "error": error,
        },
    )


def _last_mcp_call(log_path: Path) -> dict[str, Any] | None:
    if not log_path.exists():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-50:]):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "mcp_call":
            return rec
    return None


def log_agent_fallback(
    log_path: Path,
    tool_name: str,
    target: str,
) -> None:
    """Record read_file or shell grep/find calls and correlate with recent MCP calls."""
    prev = _last_mcp_call(log_path)
    followed_mcp: str | None = None
    seconds_since: float | None = None
    if prev and "ts" in prev:
        try:
            prev_ts = datetime.fromisoformat(prev["ts"])
            delta = (_now() - prev_ts).total_seconds()
            if 0 <= delta <= 120:
                followed_mcp = prev.get("tool")
                seconds_since = round(delta, 2)
        except ValueError:
            pass

    append_event(
        log_path,
        {
            "event": "agent_fallback",
            "tool": tool_name,
            "target": target[:240],
            "followed_mcp_tool": followed_mcp,
            "seconds_since_mcp": seconds_since,
        },
    )


def summarize_metrics(log_path: Path, recent_limit: int = 15) -> dict[str, Any]:
    """Summarize MCP call counts, average payload sizes, and fallback frequency."""
    if not log_path.exists():
        return {
            "total_mcp_calls": 0,
            "total_fallbacks": 0,
            "by_tool": {},
            "fallbacks_after_mcp": {},
            "recent": [],
        }

    records: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    by_tool: dict[str, dict[str, Any]] = {}
    fallbacks_after: Counter[str] = Counter()
    total_mcp = 0
    total_fallbacks = 0

    for rec in records:
        ev = rec.get("event")
        if ev == "mcp_call":
            total_mcp += 1
            t = rec.get("tool", "unknown")
            bucket = by_tool.setdefault(t, {"calls": 0, "errors": 0, "total_bytes": 0, "total_ms": 0.0})
            bucket["calls"] += 1
            if not rec.get("ok", True):
                bucket["errors"] += 1
            bucket["total_bytes"] += int(rec.get("result_bytes", 0))
            bucket["total_ms"] += float(rec.get("duration_ms", 0.0))
        elif ev == "agent_fallback":
            total_fallbacks += 1
            if rec.get("followed_mcp_tool"):
                fallbacks_after[rec["followed_mcp_tool"]] += 1

    compact_by_tool = {
        tool: {
            "calls": b["calls"],
            "errors": b["errors"],
            "avg_bytes": round(b["total_bytes"] / b["calls"], 1) if b["calls"] else 0,
            "avg_ms": round(b["total_ms"] / b["calls"], 1) if b["calls"] else 0,
        }
        for tool, b in sorted(by_tool.items())
    }

    return {
        "total_mcp_calls": total_mcp,
        "total_fallbacks": total_fallbacks,
        "by_tool": compact_by_tool,
        "fallbacks_after_mcp": dict(fallbacks_after),
        "recent": records[-recent_limit:],
    }
