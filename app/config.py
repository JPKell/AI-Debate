"""
Purpose: Load application settings and openWeightGym endpoint credentials from .env and environment variables.
Role: util
Patterns: none
Depends on: os, pathlib, dataclasses
Exposes: AppConfig, load_config, parse_dotenv
Tests: tests/test_llm_client.py
Invariants: Never expose owg_api_key in logs, reprs, or serialized API responses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def parse_dotenv(env_path: Path) -> dict[str, str]:
    """Parse key=value pairs from a .env file without mutating os.environ.

    Parameters:
        env_path: Path to the .env file.
    Returns:
        Dictionary of stripped environment variable names and values.
    Side effects:
        Reads env_path from disk if it exists.
    """
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        if key:
            values[key] = val
    return values


@dataclass(frozen=True)
class AppConfig:
    """Immutable runtime configuration for AI-Debate.

    Parameters:
        owg_protocol: HTTP protocol for openWeightGym (`http` or `https`).
        owg_host: Hostname or IP of the openWeightGym control node.
        owg_port: Port of the openWeightGym model API listener.
        owg_api_key: Bearer token for authenticating to openWeightGym (redacted in repr).
        suite_application: Label sent in X-Suite-Application header.
        data_dir: Directory holding SQLite database and runtime files.
        host: Bind host for Uvicorn server.
        port: Bind port for Uvicorn server.
    Returns:
        Frozen AppConfig instance.
    Side effects:
        None.
    """

    owg_protocol: str = "http"
    owg_host: str = "127.0.0.1"
    owg_port: int = 8442
    owg_api_key: str = field(default="", repr=False)
    app_secret_key: str = field(default="", repr=False)
    suite_application: str = "ai-debate"
    data_dir: Path = Path("data")
    host: str = "0.0.0.0"
    port: int = 8008

    @property
    def owg_base_url(self) -> str:
        """Return normalized base URL for the openWeightGym model API."""
        return f"{self.owg_protocol}://{self.owg_host}:{self.owg_port}"

    @property
    def db_path(self) -> Path:
        """Return absolute or relative path to the SQLite database file."""
        return self.data_dir / "ai_debate.db"


def load_config(project_root: Path | None = None) -> AppConfig:
    """Load AppConfig from .env with os.environ overrides taking precedence.

    Parameters:
        project_root: Optional workspace root containing .env; defaults to repository root.
    Returns:
        Populated AppConfig instance.
    Side effects:
        Reads .env from disk if present.
    """
    root = project_root or Path(__file__).resolve().parent.parent
    dotenv_values = parse_dotenv(root / ".env")

    def get_var(*names: str, default: str = "") -> str:
        for name in names:
            env_val = os.environ.get(name)
            if env_val is not None and env_val.strip():
                return env_val.strip()
        for name in names:
            file_val = dotenv_values.get(name)
            if file_val is not None and file_val.strip():
                return file_val.strip()
        return default

    protocol = get_var("OWG_PROTOCOL", default="http").lower()
    if protocol not in {"http", "https"}:
        protocol = "http"

    host = get_var("OWG_HOST", "API_IP", default="127.0.0.1")
    raw_port = get_var("OWG_PORT", "API_PORT", default="8442")
    try:
        owg_port = int(raw_port)
    except ValueError:
        owg_port = 8442

    api_key = get_var("OWG_API_KEY", "API_KEY", default="")
    app_secret = get_var("APP_SECRET_KEY", default="")
    suite_app = get_var("SUITE_APPLICATION", default="ai-debate")

    data_dir_raw = get_var("AI_DEBATE_DATA_DIR", default=str(root / "data"))
    data_dir = Path(data_dir_raw)

    app_host = get_var("AI_DEBATE_HOST", default="0.0.0.0")
    try:
        app_port = int(get_var("AI_DEBATE_PORT", default="8008"))
    except ValueError:
        app_port = 8008

    return AppConfig(
        owg_protocol=protocol,
        owg_host=host,
        owg_port=owg_port,
        owg_api_key=api_key,
        app_secret_key=app_secret,
        suite_application=suite_app,
        data_dir=data_dir,
        host=app_host,
        port=app_port,
    )
