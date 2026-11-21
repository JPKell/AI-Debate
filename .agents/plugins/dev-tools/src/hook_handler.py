"""Multi-agent lifecycle hook handler (Antigravity + Claude Code) for guardrails, sync, and telemetry."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .indexer import reindex_project
from .observability import log_agent_fallback

RAW_PYTEST_RE = re.compile(r"(^|[;&|\s])(python3?\s+-m\s+)?pytest(\s|$)")
RAW_RUFF_RE = re.compile(r"(^|[;&|\s])ruff\s+(check|format)(\s|$)")
PROTECTED_FILENAMES = {".env", ".env.local", ".env.production", "secrets.json"}


def _read_stdin_payload() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _is_claude_payload(payload: dict[str, Any]) -> bool:
    return "tool_name" in payload or "hook_event_name" in payload


def _resolve_root_from_payload(payload: dict[str, Any]) -> Path | None:
    workspaces = payload.get("workspacePaths")
    if isinstance(workspaces, list) and workspaces:
        return Path(workspaces[0])
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    return None


def _extract_tool_info(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if "toolCall" in payload and isinstance(payload["toolCall"], dict):
        tc = payload["toolCall"]
        return str(tc.get("name", "")), dict(tc.get("args") or {})
    return str(payload.get("tool_name", "")), dict(payload.get("tool_input") or {})


def _extract_path_arg(args: dict[str, Any]) -> str:
    for key in ("Path", "file_path", "path"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_cmd_arg(args: dict[str, Any]) -> str:
    for key in ("CommandLine", "command"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _deny_response(payload: dict[str, Any], reason: str) -> dict[str, Any]:
    if _is_claude_payload(payload):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    return {"decision": "deny", "reason": reason}


def _allow_response(payload: dict[str, Any]) -> dict[str, Any]:
    if _is_claude_payload(payload):
        return {}
    return {"decision": "allow"}


def handle_pre_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Enforce Tool-First Never list: block edits to index/secrets and raw shell pytest/ruff."""
    try:
        cfg = load_config(_resolve_root_from_payload(payload))
        tool_name, args = _extract_tool_info(payload)

        if tool_name in {"write_file", "edit_file", "Write", "Edit", "MultiEdit"}:
            path_str = _extract_path_arg(args)
            if path_str:
                target = Path(path_str)
                resolved = target.resolve() if target.is_absolute() else (cfg.project_root / target).resolve()
                try:
                    rel = resolved.relative_to(cfg.project_root).as_posix()
                except ValueError:
                    rel = path_str

                if rel == ".agents/state" or rel.startswith(".agents/state/"):
                    return _deny_response(
                        payload,
                        "Blocked by Tool-First guardrail: never hand-edit .agents/state/ index or logs. "
                        "Use MCP tools (reindex, index_status, tool_metrics) instead.",
                    )
                if Path(rel).name in PROTECTED_FILENAMES:
                    return _deny_response(
                        payload,
                        f"Blocked by Tool-First guardrail: do not edit secret file '{Path(rel).name}'. "
                        "Ask the user to update secrets manually.",
                    )

        if tool_name in {"run_command", "Bash"}:
            cmd = _extract_cmd_arg(args)
            # Allow toolkit self-setup and toolkit self-tests inside .agents/plugins/dev-tools
            if ".agents/plugins/dev-tools" in cmd:
                return _allow_response(payload)

            if RAW_PYTEST_RE.search(cmd):
                return _deny_response(
                    payload,
                    "Blocked by Tool-First guardrail: use MCP tool 'run_tests' instead of shell pytest "
                    "for compact, machine-readable failure output.",
                )
            if RAW_RUFF_RE.search(cmd):
                return _deny_response(
                    payload,
                    "Blocked by Tool-First guardrail: use MCP tool 'lint_and_headers' instead of shell ruff "
                    "so both Ruff lint and module headers are verified together.",
                )
    except Exception as exc:
        print(f"[dev-tools hook:pre_tool] {exc}", file=sys.stderr)
    return _allow_response(payload)


def handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    """Incrementally update the knowledge graph (including OpenWeightGym summaries/embeddings) after each turn."""
    try:
        cfg = load_config(_resolve_root_from_payload(payload))
        reindex_project(config=cfg, full=False, sync_remote=True, trigger="hook:stop")
    except Exception as exc:
        print(f"[dev-tools hook:stop] {exc}", file=sys.stderr)
    if _is_claude_payload(payload):
        return {}
    return {"decision": "allow"}


def handle_post_tool(payload: dict[str, Any]) -> dict[str, Any]:
    """Fast local AST/header sync after writes; fallback telemetry on reads/shell grep."""
    try:
        cfg = load_config(_resolve_root_from_payload(payload))
        tool_name, args = _extract_tool_info(payload)

        if tool_name in {"write_file", "edit_file", "Write", "Edit", "MultiEdit"}:
            path_str = _extract_path_arg(args)
            if path_str:
                try:
                    p = Path(path_str)
                    resolved = p.resolve() if p.is_absolute() else (cfg.project_root / p).resolve()
                    rel = resolved.relative_to(cfg.project_root)
                    if rel.suffix in cfg.extensions and any(
                        rel.as_posix().startswith(f"{d}/") for d in cfg.source_dirs
                    ):
                        # Fast local-only AST/header refresh mid-turn; remote OWG sync runs on Stop
                        reindex_project(
                            config=cfg,
                            full=False,
                            sync_remote=False,
                            trigger=f"hook:{tool_name}",
                        )
                except ValueError:
                    pass
        elif tool_name in {"read_file", "Read"}:
            target = _extract_path_arg(args)
            if target and ".agents/state" not in target:
                log_agent_fallback(cfg.usage_log_path, tool_name, target)
        elif tool_name in {"run_command", "Bash", "Grep"}:
            cmd = _extract_cmd_arg(args) or str(args.get("pattern", ""))
            if tool_name == "Grep" or any(tok in cmd for tok in ("grep ", "rg ", "find ", "pytest", "ruff")):
                log_agent_fallback(cfg.usage_log_path, tool_name, cmd)
    except Exception as exc:
        print(f"[dev-tools hook:post_tool] {exc}", file=sys.stderr)
    return {}


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
    payload = _read_stdin_payload()
    if mode == "pre_tool":
        out = handle_pre_tool(payload)
    elif mode == "post_tool":
        out = handle_post_tool(payload)
    else:
        out = handle_stop(payload)
    # stdout MUST contain only the valid JSON hook response
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
