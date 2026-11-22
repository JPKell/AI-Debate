# AI-Debate

A self-hosted Python web application that orchestrates real-time, streaming back-and-forth debates between two AI models served via an OpenAI-compatible **openWeightGym** API.

## Features

- **Live Streaming 2-Model Arena**: 3-column responsive workspace (Model A | Debate Feed + Moderator Bar | Model B) with collapsible Debates History drawer and mobile slide-over panels.
- **Dynamic openWeightGym Discovery**: Fetches chat-capable models from `GET /v1/models`, displaying `guaranteed–max` context windows, live `loaded_context`, node status (`Node may wake`), streaming capability, adaptor guards, and per-model `reasoning_effort` levels.
- **Thinking Controls & Auto-Collapse**: Per-model thinking mode (`Model default`, `On`, `Off`) and reasoning effort selectors; parses both `delta.reasoning_content` and inline `<think>...</think>` blocks, streaming live and auto-collapsing once answer tokens begin.
- **Rich Prompt Composition & Roleplay Controls**: Style presets (`Adversarial`, `Cooperative`, `Explorative`, `Custom` with per-user saved presets), 5 conviction levels (`Immovable` → `Synthesis-seeking`), Side/Position, per-debater roleplay rules (**Talk to other model only**, **No thoughts as actions**, **Let Narrator describe actions**), live Composed System Prompt preview with optional manual override, and strict role-flipped history (private thinking is never leaked to any model).
- **Optional 3rd Action Narrator Model**: Enable a third model to narrate physical actions and scene beats between debater turns with configurable cadence: **After every turn** (`A → Action → B → Action`), **Every N turns**, or **Random `randint(1, N)` turns** (narrator beats do not consume the A/B debater turn limit).
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

## Production Deployment & Auto-Start on Boot

### Option 1: Docker Compose (Recommended)

The production [Dockerfile](./Dockerfile) and [docker-compose.yml](./docker-compose.yml) run AI-Debate as a non-root user with a persistent SQLite/secret-key volume (`ai_debate_data`) and `restart: unless-stopped` so it starts automatically whenever the computer boots:

```bash
# Ensure Docker itself starts on boot (Linux)
sudo systemctl enable --now docker

# Build and start AI-Debate in the background
docker compose up -d --build

# Check health & logs
docker compose ps
docker compose logs -f ai-debate
```

Or with plain `docker run`:

```bash
docker build -t ai-debate:latest .
docker run -d \
  --name ai-debate \
  --restart unless-stopped \
  -p 8008:8008 \
  -v ai-debate-data:/app/data \
  --env-file .env \
  ai-debate:latest
```

> **Note:** `--env-file .env` is optional—you can omit it and configure your openWeightGym host, port, and encrypted API key directly in the **Admin** panel after creating your initial admin account at `http://<host>:8008`.

### Option 2: Native Linux `systemd` Service (Without Docker)

To run `python run.py` directly on startup via `systemd`, replace `/path/to/AI-Debate` and `<your-user>` below:

```bash
sudo tee /etc/systemd/system/ai-debate.service > /dev/null <<'EOF'
[Unit]
Description=AI-Debate Web Application
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/path/to/AI-Debate
ExecStart=/path/to/AI-Debate/.venv/bin/python /path/to/AI-Debate/run.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ai-debate
sudo systemctl status ai-debate
```

## Running Tests

```bash
pytest -q
```

When using the sandboxed dev container (`DevDockerfile`), dependencies (including Playwright & Chromium for browser E2E audits) are pre-installed in `/opt/app-venv`, and Tool-First Dev MCP checks (`run_tests`, `lint_and_headers`, `index_status`) run via `/opt/dev-tools-venv`.

## Architecture

See [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) for the complete architecture, SQLite schema, openWeightGym header/error matrix, and module layout.
