# AI-Debate

A self-hosted Python web application that orchestrates real-time, streaming back-and-forth debates between two AI models served via an OpenAI-compatible **openWeightGym** API.

## Features

- **Live Streaming 2-Model Arena**: 3-column responsive workspace (Model A | Debate Feed + Moderator Bar | Model B) with collapsible Debates History drawer and mobile slide-over panels.
- **Dynamic openWeightGym Discovery**: Fetches chat-capable models from `GET /v1/models`, displaying `guaranteed–max` context windows, live `loaded_context`, node status (`Node may wake`), streaming capability, adaptor guards, and per-model `reasoning_effort` levels.
- **Thinking Controls & Auto-Collapse**: Per-model thinking mode (`Model default`, `On`, `Off`) and reasoning effort selectors; parses both `delta.reasoning_content` and inline `<think>...</think>` blocks, streaming live and auto-collapsing once answer tokens begin.
- **Rich Prompt Composition**: Style presets (`Adversarial`, `Cooperative`, `Explorative`, `Custom` with per-user saved presets), 5 conviction levels (`Immovable` → `Synthesis-seeking`), Side/Position, live Composed System Prompt preview with optional manual override, and strict role-flipped history (private thinking is never leaked to either model).
- **Context Compaction (Auto + Manual + Reactive)**: Live context meters on both panels, automatic rolling summarization before hitting active context limits, manual **Compact** trigger, and reactive recovery on HTTP `400 context_too_small`.
- **Global Single-Debate Lock & Reconnection**: Enforces at most one active debate globally (`409 Conflict` with same-user jump link and cross-user redaction), buffers in-progress streams for seamless refresh/reconnection, and provides Admin emergency force-stop.
- **Multi-User Auth & Admin Console**: First registered user becomes `admin`; `scrypt` password hashing, signed `HttpOnly` sessions, Fernet-encrypted API key at rest, Test Connection, optional openWeightGym headers (`X-Suite-Application`, `X-LoadCoach-Priority`, `X-LoadCoach-Sensitivity`, `X-LoadCoach-Log: off`, `X-Suite-Node`, `X-Turnstile-No-Wake`), and user management.
- **Controls, Forking, Export & Themes**: Start / Pause / Resume / Stop / Continue (`+5 Turns`, `+10 Turns`, `+5 Min`, Custom), Moderator interjections, Duplicate or Fork & Swap Sides, Markdown/JSON export with thinking/stats toggles, and 5 switchable themes (`Obsidian`, `Daylight`, `Midnight Neon`, `Warm Paper`, `Forest Terminal`).

## Quick Start

1. **Configure environment** (optional — can also be configured in the Admin UI after first login):
   ```bash
   cp .env.example .env
   # Edit .env with your OWG_PROTOCOL / OWG_HOST / OWG_PORT / OWG_API_KEY
   ```

2. **Install dependencies**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Run the server**:
   ```bash
   python run.py
   ```
   Open `http://127.0.0.1:8008` in your browser (override bind host/port/data dir via `AI_DEBATE_HOST`, `AI_DEBATE_PORT`, `AI_DEBATE_DATA_DIR`). The first account you register becomes the **Admin**, where you can configure and test your openWeightGym endpoint under **Admin**.

## Running Tests

```bash
pytest -q
```

When using the sandboxed dev container (`DevDockerfile`), dependencies are pre-installed in `/opt/app-venv` and Tool-First Dev MCP checks (`run_tests`, `lint_and_headers`, `index_status`) run via `/opt/dev-tools-venv`.

## Architecture

See [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) for the complete architecture, SQLite schema, openWeightGym header/error matrix, and module layout.
