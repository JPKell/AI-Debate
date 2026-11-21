"""Single-command launcher for the AI-Debate MVP server."""

from __future__ import annotations

import uvicorn

from app.config import load_config


def main() -> None:
    """Load configuration from .env and run Uvicorn."""
    cfg = load_config()
    print(f"Starting AI-Debate at http://{cfg.host}:{cfg.port} -> openWeightGym: {cfg.owg_base_url}")
    uvicorn.run(
        "app.main:app",
        host=cfg.host,
        port=cfg.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
