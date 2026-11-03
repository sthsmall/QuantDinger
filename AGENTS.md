# QuantDinger Repository Instructions

Canonical agent instructions. `CLAUDE.md` only imports this file; do not duplicate rules in tool-specific files.

## Repository map

- `backend_api_python/`: the only application source — Flask API (`run.py`, gunicorn), services, trading runtime, migrations, tests.
- `mcp_server/`: standalone `quantdinger-mcp` package, a tenant-scoped client over `/api/agent/v1`. Own version (`0.6.2`) and own CI.
- `docs/`: architecture, trading, Agent Gateway, API, deployment docs. Only `README.md` / `README_CN.md` may sit at the `docs/` root (`scripts/check_docs.py` enforces this).
- `ops/`: Prometheus / Grafana / Alertmanager config, used only by `docker-compose.observability.yml`.
- `docker-compose*.yml`, `install.sh`, `install.ps1`, `scripts/`: deployment topology and repo tooling.
- Frontend and mobile source live in private repos; this repo only references prebuilt GHCR images.

## Setup and tests

Work from `backend_api_python/` for backend changes. Python 3.12 is the reference (Docker base image, CI matrix, ruff target).

```bash
cd backend_api_python
python -m venv .venv                       # .venv\Scripts\activate on Windows
pip install -r requirements.lock -r requirements-dev.txt
python -m pytest tests/test_<name>.py -q
```

- `pytest.ini` sets a global 120s timeout and defines the `integration` and `stress` markers. Both are opt-in: CI runs `-m "not integration and not stress"`.
- `tests/conftest.py` injects test env and `SKIP_STARTUP_HOOKS=1`, so app creation does not touch the database. Any test that actually uses the DB needs a reachable Postgres — there is no SQLite fallback (`get_db_type()` always returns `postgresql`).
- For the full suite locally, use a disposable Postgres on a non-default port so you never touch the live compose DB:

```bash
docker run -d --rm --name qd-test-pg -p 5433:5432 \
  -e POSTGRES_USER=quantdinger_test -e POSTGRES_PASSWORD=quantdinger_test \
  -e POSTGRES_DB=quantdinger_test postgres:18-alpine
export DATABASE_URL=postgresql://quantdinger_test:quantdinger_test@127.0.0.1:5433/quantdinger_test
python -m app.commands.migrate
python -m pytest tests/ -q -m "not integration and not stress"
```

- Postgres integration tests skip unless `QD_TEST_POSTGRES_DSN` points at a migrated database; release gates run separately with `python -m pytest tests/release_gate -q`.
- MCP: `pip install -e './mcp_server[dev]'` then `python -m pytest mcp_server/tests -q` from the repo root.
- Do not commit `.venv/`, `install.log`, or other local build artifacts.

## Checks CI enforces

Run the ones matching your change; commands below are from `backend_api_python/` unless noted.

- `ruff check app scripts tests` — only `E9`, `F63`, `F7`, `F82` are enabled (`pyproject.toml`). This is a critical-syntax gate, not a style gate; do not reformat or chase style findings.
- `python -m compileall -q app scripts tests`
- `python scripts/backend_quality_check.py` — enforces the file/function-size baseline and banned exchange tokens; new code must not grow baselined hotspots.
- `python scripts/check_requirements_lock.py` — every direct `requirements.txt` entry needs one exact `==` pin in `requirements.lock`. Add the pin when you add a dependency.
- `python scripts/export_openapi.py` (no args writes `docs/api/openapi.yaml`) — the committed spec must match the Flask-Smorest export; CI diffs them.
- From the repo root: `python scripts/check_docs.py` (doc layout, local links, code fences), `python scripts/check_version.py`, `python scripts/check_mojibake.py` (tracked text must stay UTF-8; no mojibake).
- Compose: `docker compose -f docker-compose.yml config -q`, plus the ghcr / build / observability / production override combinations listed in `basic-ci.yml`.

## Architecture invariants

- Process roles are selected by `QD_PROCESS_ROLE` (`app/runtime/roles.py`): `api`, `trading`, `scheduler`, `celery`, `kafka-audit`, `strategy-dispatcher`, `strategy-evaluator`. The default `legacy` runs everything in one process and is for local dev only. In Docker each role is a separate compose service sharing the backend image.
- Never run persistent trading loops or submit exchange orders from the API process. The evaluator decides; the trading worker executes. Preserve leases, fencing, and idempotency when touching runtime code.
- Postgres is the durable source of truth, Kafka carries versioned events, the cache Redis is evictable, and `redis-jobs` has a separate durability role. Do not substitute one for another.
- `docs/architecture/` is the authority on runtime ownership and the V5.x evolution; read the document covering the area you touch before restructuring it.

## Gotchas

- **Migrations are not auto-discovered.** Each SQL file in `migrations/` must be registered in `_apply_init_sql()` in `app/utils/db.py`. Applied components are tracked by SHA-256 checksum, so editing an already-applied file makes it re-run on the next boot.
- Deploy-time entrypoints are `python -m app.commands.migrate` (strict) and `python -m app.commands.kafka_topics`. Compose orders `env-sync` → `migration` → `kafka-init` → backend/workers; a new module that reads a topic needs it created there.
- Root `.env` is the single config for Compose and every backend process, and it is gitignored. On `docker compose up`, the `env-sync` service appends missing keys from `.env.example` and writes `.env.bak.<timestamp>`. Never put local values in `.env.example`.
- Version consistency is checked: root `VERSION` and `backend_api_python/VERSION` must match. Bump both with `python scripts/bump_version.py X.Y.Z`. Release images take their version from the git tag or the `APP_VERSION` / `GIT_TAG` build args, not from the `VERSION` file.
- `docker compose up -d --build` builds the backend from local source but pulls frontend/mobile from GHCR. Building the frontend locally requires `-f docker-compose.build.yml` and a `./QuantDinger-Vue/` checkout.
- The default stack requires Kafka. Without it, `kafka-init`, `kafka-audit-worker`, `strategy-dispatcher-worker`, and `strategy-evaluator-worker` cannot start.

## API contracts

- `/api/agent/v1` is the Agent Gateway boundary; routes live in `app/routes/agent_v1/`.
- `docs/agent/agent-openapi.json` is hand-maintained, and `tests/test_ai_agent_contract_alignment.py` asserts exact operation-set equality with the Flask route table (plus MCP tool parity). Add or remove the documented operation in the same change, or the test fails.
- The human spec `docs/api/openapi.yaml` is generated, never hand-edited. Agent spec lints with `.spectral.agent.yaml`; human spec lints with `.spectral.yaml`.
- MCP tools must keep the backing API's authentication, scopes, idempotency, limits, and live-trading confirmations.

## Safety and hygiene

- Never commit secrets, real `.env` files, API keys, or agent tokens. `opencode.json` is gitignored and holds a local MCP agent token — keep it out of git.
- Do not weaken live-trading safeguards or bypass explicit authorization / human review unless the user scopes that change.
- Do not add upgrade-time data rewrites for one-off local cleanup; use an explicit reviewed migration only when shipped user data must change.
- Preserve unrelated working-tree changes.
- English for source comments and identifiers. User-facing copy goes through the localization system rather than hardcoded strings.
- Docs ship as EN / `_CN` pairs; update both when the change affects both audiences.

## Read by task

- Agent Gateway or MCP: `docs/agent/README.md`, `docs/agent/agent-openapi.json`, `mcp_server/README.md`.
- Strategy API, backtests, trading behavior: `docs/trading/STRATEGY_DEV_GUIDE.md`, `docs/trading/LIVE_TRADING_SAFETY.md`.
- Runtime ownership, concurrency, Kafka, workers: `docs/architecture/README.md` and the linked document.
- HTTP conventions: `docs/architecture/API_CONVENTIONS.md`.
- Install and operations: `README.md`, `DEVELOPMENT.md`, `docs/deployment/`.
