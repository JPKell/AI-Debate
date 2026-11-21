"""
Purpose: Integration tests for first-user admin bootstrap, authentication, per-user debate isolation, admin-only controls, and encrypted API key storage.
Role: test
Patterns: contract-first-test, thin-router, repository-pattern
Depends on: app.config, app.main, app.repositories.db, app.repositories.settings_repo, app.security, httpx
Exposes: test_first_user_admin_bootstrap_and_user_isolation, test_admin_settings_encrypts_api_key_at_rest, test_custom_prompts_per_user_isolation
Tests: tests/test_auth_and_roles.py
Invariants: Uses isolated temporary directories and mocked upstream HTTP; never exposes raw API keys or password hashes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from app.config import AppConfig
from app.main import create_app


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        owg_protocol="http",
        owg_host="mock-owg",
        owg_port=8442,
        owg_api_key="super-secret-env-key-12345",
        suite_application="ai-debate",
        data_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_first_user_admin_bootstrap_and_user_isolation(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    app = create_app(cfg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as admin_client,
            httpx.AsyncClient(transport=transport, base_url="http://test") as user_client,
            httpx.AsyncClient(transport=transport, base_url="http://test") as anon_client,
        ):
            # Unauthenticated API requests are rejected
            r_unauth = await anon_client.get("/api/debates")
            assert r_unauth.status_code == 401

            # First registered user automatically becomes admin
            r_reg_admin = await admin_client.post(
                "/api/auth/register",
                json={"username": "alice", "password": "password123"},
            )
            assert r_reg_admin.status_code == 201
            assert r_reg_admin.json()["role"] == "admin"
            assert "password_hash" not in r_reg_admin.text

            # Second registered user becomes regular user
            r_reg_user = await user_client.post(
                "/api/auth/register",
                json={"username": "bob", "password": "password456"},
            )
            assert r_reg_user.status_code == 201
            assert r_reg_user.json()["role"] == "user"

            # Regular user cannot access admin endpoints
            assert (await user_client.get("/api/admin/settings")).status_code == 403
            assert (await user_client.post("/api/admin/force-stop")).status_code == 403

            # Admin can access admin settings (and API key is never returned in plain text)
            r_settings = await admin_client.get("/api/admin/settings")
            assert r_settings.status_code == 200
            settings_data = r_settings.json()
            assert settings_data["api_host"] == "mock-owg"
            assert settings_data["api_key_configured"] is True
            assert "super-secret-env-key-12345" not in r_settings.text

            # Alice creates a debate; Bob cannot read, edit, or delete it
            r_deb = await admin_client.post(
                "/api/debates",
                json={"title": "Alice Secret Debate", "scene_prompt": "Private topic"},
            )
            assert r_deb.status_code == 201
            alice_debate_id = r_deb.json()["id"]

            assert (await user_client.get(f"/api/debates/{alice_debate_id}")).status_code == 404
            assert (
                await user_client.patch(f"/api/debates/{alice_debate_id}", json={"title": "Hacked"})
            ).status_code == 404
            assert (await user_client.delete(f"/api/debates/{alice_debate_id}")).status_code == 404
            assert (await user_client.get("/api/debates")).json() == []


@pytest.mark.asyncio
async def test_admin_settings_encrypts_api_key_at_rest(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    app = create_app(cfg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin_client:
            await admin_client.post(
                "/api/auth/register",
                json={"username": "admin1", "password": "strongpassword"},
            )
            r_upd = await admin_client.patch(
                "/api/admin/settings",
                json={
                    "api_host": "10.0.0.9",
                    "api_port": 9000,
                    "api_key": "brand-new-plaintext-key-xyz",
                    "log_off": True,
                    "default_priority": "high",
                },
            )
            assert r_upd.status_code == 200
            assert "brand-new-plaintext-key-xyz" not in r_upd.text
            assert r_upd.json()["api_host"] == "10.0.0.9"
            assert r_upd.json()["log_off"] is True

        # Inspect SQLite directly to prove key is encrypted at rest
        conn = sqlite3.connect(str(cfg.db_path))
        row = conn.execute("SELECT api_key_encrypted FROM app_settings WHERE id = 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] is not None
        assert "brand-new-plaintext-key-xyz" not in row[0]
        assert "super-secret-env-key-12345" not in row[0]

        # Runtime settings decrypts cleanly for the LLM client
        runtime = app.state.settings_repo.get_runtime_settings()
        assert runtime.api_key == "brand-new-plaintext-key-xyz"
        assert runtime.base_url == "http://10.0.0.9:9000"


@pytest.mark.asyncio
async def test_custom_prompts_per_user_isolation(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    app = create_app(cfg)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as client_a,
            httpx.AsyncClient(transport=transport, base_url="http://test") as client_b,
        ):
            await client_a.post("/api/auth/register", json={"username": "u1", "password": "password123"})
            await client_b.post("/api/auth/register", json={"username": "u2", "password": "password123"})

            r_create = await client_a.post(
                "/api/prompts",
                json={
                    "name": "Socratic Judge",
                    "style_category": "explorative",
                    "content": "Ask incisive questions before stating conclusions.",
                },
            )
            assert r_create.status_code == 201
            pid = r_create.json()["id"]

            list_a = (await client_a.get("/api/prompts")).json()["custom_prompts"]
            list_b = (await client_b.get("/api/prompts")).json()["custom_prompts"]
            assert len(list_a) == 1
            assert len(list_b) == 0

            # User A can update their own custom prompt; User B cannot
            r_patch = await client_a.patch(
                f"/api/prompts/{pid}",
                json={"content": "Use sharp Socratic cross-examination."},
            )
            assert r_patch.status_code == 200
            assert r_patch.json()["content"] == "Use sharp Socratic cross-examination."
            assert (
                await client_b.patch(f"/api/prompts/{pid}", json={"content": "Hijacked"})
            ).status_code == 404

            # User B cannot delete User A's custom prompt
            assert (await client_b.delete(f"/api/prompts/{pid}")).status_code == 404

            # Admin (client_a) can reset User B's password and User B can sign in with it
            users = (await client_a.get("/api/admin/users")).json()
            u2 = next(u for u in users if u["username"] == "u2")
            r_reset = await client_a.patch(
                f"/api/admin/users/{u2['id']}",
                json={"password": "newpassword789"},
            )
            assert r_reset.status_code == 200
            r_login_old = await client_b.post(
                "/api/auth/login", json={"username": "u2", "password": "password123"}
            )
            assert r_login_old.status_code == 401
            r_login_new = await client_b.post(
                "/api/auth/login", json={"username": "u2", "password": "newpassword789"}
            )
            assert r_login_new.status_code == 200

            # Authenticated workspace page renders core controls
            r_page = await client_a.get("/")
            assert r_page.status_code == 200
            for dom_id in (
                "debates-search",
                "a-prompt-preview",
                "a-prompt-override",
                "a-seed",
                "continue-group",
                "fork-menu",
                "export-menu",
            ):
                assert f'id="{dom_id}"' in r_page.text
