# AGENTS.md

These instructions apply to the whole repository. A nested `AGENTS.md` may
override them for a more specific area.

## Project

MRAZ Master is a Django 5.2 / Python 3.13 application for tabletop and text RPG
sessions with a human game master, LLM or human players, and controlled turn
execution. The normal runtime is Docker Compose with PostgreSQL, Uvicorn, and a
LiteLLM proxy. The UI uses Django templates, HTMX, minimal Alpine.js, and plain
CSS. `browser_bridge/` is a build-free Manifest V3 extension.

The primary architectural invariant is that models never initiate calls to
other models. Saving a message must not trigger a turn. Only the Turn Engine,
following an authorized master action or configured continuation flow, may
initiate model work.

## Repository map

- `mraz/`: Django settings, root URLs, and ASGI/WSGI entry points.
- `rpg/models.py`: data model and domain constraints.
- `rpg/services/`: context building, LLM adapters, GM orchestration,
  summaries, and Turn Engine logic.
- `rpg/views.py`, `rpg/forms.py`, `rpg/urls.py`: HTTP boundary.
- `rpg/templates/rpg/` and `rpg/static/rpg/`: server-rendered UI.
- `rpg/tests/`: pytest/pytest-django tests and factories.
- `rpg/management/commands/seed_demo.py`: idempotent demo-data seeding.
- `browser_bridge/`: Chromium/Edge extension and provider adapters.
- `compose.yaml`, `Dockerfile`, `litellm_config.yaml`: runtime setup.
- `deploy/`: public player-edge proxy configuration.

## Common commands

Use the Docker workflow documented in `README.md`:

```bash
cp .env.example .env
docker compose up --build
docker compose run --rm --build web-test pytest
docker compose run --rm --build web-test pytest rpg/tests/test_turn_engine.py
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web python manage.py seed_demo
```

Run focused tests while iterating, then the full suite before handoff when
practical. Tests must be deterministic and offline; use mock mode rather than
real provider credentials.

## Implementation guidelines

- Keep views thin. Put orchestration, privacy filtering, state transitions, and
  context assembly in focused service functions.
- Follow existing Python style: four spaces, useful type hints, and the
  100-character line limit configured in `pyproject.toml`.
- Preserve transaction and locking behavior around turns and executions.
  Concurrency changes need regression tests for duplicate submits, retries, and
  partial failures.
- Do not bypass response parsers or validators. API, manual-chat, browser
  bridge, and human transports should share validation and persistence rules
  where their contracts overlap.
- Keep server-side authorization and legal-action checks authoritative even
  when the UI hides a control.
- Keep HTMX polling fragments independently refreshable. Do not put live inputs
  in polling fragments unless draft preservation is explicitly handled.
- Keep browser-bridge provider DOM adapters isolated and preserve the manual
  Copy/Open/Paste fallback.
- Never edit historical migrations. Add a migration for model changes and
  verify it.
- Update `README.md` and `.env.example` when setup, configuration, deployment,
  or user workflows change.

## Domain invariants

- Message visibility is `PUBLIC`, `PRIVATE_GM_PLAYER`, or `GM_ONLY`. Public
  means public only to participants of that scene. Do not leak private data
  through context, inherited history, search, debug, or player views.
- A player inherits predecessor history and memory only from scenes in which
  that player participated.
- Public multi-player executions use frozen context snapshots. A same-round
  response must not enter another player's frozen context.
- Model calls and round advancement belong to Turn Engine paths, not model
  signals, generic saves, or template requests.
- Private GM-player turns do not advance the public round. ROUND order includes
  every current participant exactly once.
- Submission UUIDs and retry behavior are idempotency boundaries. Never replay
  successful executions or advance a round twice.
- Closed scenes are read-only in normal play. Scene transitions update name,
  description, and memory coherently.
- Player declarations establish their own speech, intent, and voluntary action,
  but not unconfirmed external facts or consequences.
- Length, paragraph, question-count, action-role, and bilingual-speech
  validation must remain consistent across model, manual, and human inputs.
- A `WAITING_HUMAN` or `WAITING_EXTERNAL` execution blocks the scene from
  advancing beneath that player.

## Security and privacy

- Never commit `.env`, provider keys, LiteLLM credentials, Cloudflare tokens,
  Django secrets, bearer player URLs, or production prompts/transcripts.
- Read secrets from environment variables. Add only safe placeholders to
  `.env.example`.
- Human-player tokens are credentials. Keep access scene-scoped and responses
  private/no-store; do not substitute exposed database IDs for tokens.
- Public deployment remains limited to `/play/` and approved static assets.
  Never expose GM, admin, debug, health, or scene-management routes through
  `player-edge`.
- Preserve CSRF and server-side authorization. The browser extension submits
  through Django and never accesses the database directly.
- Do not log credentials, tokens, or private content outside the explicitly
  authorized GM debug workflow.

## Testing expectations

Add regression coverage for behavioral changes:

- Turn/state changes: legal and invalid transitions, retries, idempotency,
  undo/rollback, and ROUND advancement.
- Privacy/context changes: assert both visible and excluded data, including
  predecessor-scene and human-player cases.
- Views/forms: authorization, POST validation, and relevant HTMX responses.
- Model/parser changes: malformed, invalid, and provider-failure responses.
- Human/manual transports: waiting-state blocking and shared validation.
- Browser bridge: Django contract tests plus manual adapter verification when
  provider DOM behavior changes.

Before finishing, inspect `git diff` and `git status`. Preserve unrelated user
changes and report tests run or checks that could not be run.
