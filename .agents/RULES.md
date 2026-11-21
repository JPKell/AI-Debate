# Project Rules & Named Patterns

This file defines architectural conventions, named patterns, module boundaries, and documentation rules that mechanical linters cannot check. Mechanically checkable formatting and lint rules live in `.agents/ruff.toml`.

---

## 1. Role Vocabulary

Every source module must declare **one** `Role` tag from `.agents/config.json`:

| Role | Responsibility | Boundary |
|---|---|---|
| `router` | HTTP/SSE endpoints, request parsing, auth guards, response formatting. | Thin layer only; delegate business logic to `service` and SQL to `repository`. |
| `service` | Core domain workflows, orchestration, external API clients, state transitions. | No raw FastAPI `Request`/`Response` objects; raises typed domain errors. |
| `repository` | SQLite queries, transactions, schema/migrations, encryption at rest. | Owns SQL statements; returns typed dicts/dataclasses/models, no HTTP concerns. |
| `schema` | Pydantic/dataclass DTOs, validation models, shared domain types. | Pure data definitions and validators; zero I/O or side effects. |
| `page` | Top-level UI views/templates or page-level frontend controllers. | Coordinates components and API calls; keeps layout responsive. |
| `component` | Reusable frontend UI widgets, renderers, or client helpers (e.g., SSE, Markdown). | Self-contained; communicates via callbacks/events rather than global state mutation. |
| `util` | Pure, stateless helper functions (parsing, crypto primitives, formatting). | No database, network, or framework dependencies. |
| `test` | Automated tests verifying module contracts and edge cases. | Must remain deterministic and isolated (in-memory/temp DB, mocked network). |

---

## 2. Named Patterns

Reference these exact pattern names in module headers (`Patterns: ...`) so `get_conventions` and `find_similar` can retrieve real examples.

### `thin-router`
Routers validate input with Pydantic schemas, verify authentication/authorization, invoke a `service` or `repository`, and map domain exceptions to HTTP status codes. No business loops or SQL inside route handlers.

### `service-layer`
Stateful workflows (e.g., debate lifecycle, compaction, streaming orchestration) live in dedicated service classes with explicit dependencies injected at construction time so they are unit-testable without HTTP.

### `repository-pattern`
All persistence goes through focused repository modules/functions using parameterized SQLite queries. Callers never interpolate SQL strings or share unguarded connections across threads.

### `streaming-pipeline`
External LLM streams and browser SSE streams are decoupled through an async event bus/parser. Partial chunks are normalized incrementally (separating thinking from answer tokens) and buffered so reconnecting clients catch up cleanly.

### `structured-errors`
Raise explicit domain exception types carrying a machine-readable `code`, safe user-facing `message`, and optional `status_code`. Log correlation IDs (`X-Request-ID`, `X-LoadCoach-Request-ID`), never secrets or API keys.

### `theme-tokens`
All frontend colors, spacing, and typography derive from CSS custom properties defined in `themes.css`. Never hard-code hex colors inside component styles or templates.

### `contract-first-test`
Tests are written before or alongside implementation, linked via the module's `Tests:` header field, and assert public behavior and invariants rather than private implementation details.

---

## 3. Documentation Contract

### Module Header (Required)
Every Python file under `app/` (and JS module under `app/static/js/`) must begin with a structured header containing all required fields:

- **Purpose** — One clear sentence describing what the module does.
- **Role** — Exactly one tag from the Role Vocabulary above.
- **Patterns** — Comma-separated list of pattern names from Section 2 (or `none` for trivial utilities).
- **Depends on** — Key internal/external modules this file imports (or `none`).
- **Exposes** — Public classes, functions, or routes exported by this module.
- **Tests** — Relative path(s) to covering test file(s) (or `none` only for pure entrypoints/templates).
- **Invariants** — Key assumptions or constraints that must remain true (or `none`).

### Function & Class Docstrings
Public functions and classes should concisely document:
- Purpose (first line)
- Parameters (types + constraints)
- Return value
- Side effects (DB writes, network I/O, background tasks)

---

## 4. Module Boundaries & Security Rules

- Never hard-code AI model IDs; always read available chat models from `/v1/models`.
- Never send model `thinking` text into the opposing model's conversation history.
- Never return raw API keys, password hashes, or session tokens in API responses or logs.
- Enforce user ownership checks on every debate and custom prompt endpoint; enforce `admin` role on server/user management endpoints.
