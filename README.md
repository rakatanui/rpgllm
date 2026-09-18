# MRAZ Master

A tabletop / text RPG GM interface where one Master orchestrates several
independent LLM players. Built as a vertical MVP: one Master, many LLM
players, shared public scene, private GM↔player channels, and a Turn Engine
that strictly controls who responds and when.

**Core invariant:** an LLM never initiates a call to another LLM. Saving an
AI message never triggers a new turn. Only the Turn Engine, after an explicit
Master action, initiates model calls.

## Stack

- Django 5.2 LTS / Python 3.13 / ASGI / Uvicorn
- PostgreSQL (named volume)
- LiteLLM Proxy (separate container) → OpenAI / Anthropic / Gemini / host Ollama
- Django Templates + HTMX + minimal Alpine.js + plain CSS
- Docker Compose (all runtime deps in containers; `uv` only at build time)

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

Before opening the app from a Windows browser, add this line to
`C:\\Windows\\System32\\drivers\\etc\\hosts` as Administrator:

```text
127.0.0.100 mraz.local
```

WSL's `/etc/hosts` affects WSL tools only; it does not configure name
resolution for Windows Chrome/Edge.

Open <http://mraz.local>.

Checks from WSL:

```bash
curl -I http://127.0.0.100
curl -I http://mraz.local      # only if WSL also resolves mraz.local
```

Create an admin user:

```bash
docker compose exec web python manage.py createsuperuser
```

Admin at <http://mraz.local/admin/>.

Load demo data (idempotent):

```bash
docker compose exec web python manage.py seed_demo
```

Creates Campaign **МРАЗь**, Scene **Test scene**, players **Lucien / Mila /
Mathis** with round order `Lucien → Mila → Mathis`, using MockLLM by default —
no API keys, no Ollama models required.

## Run the tests

Tests run entirely inside Docker (dev dependencies are isolated in a
`web-test` build target):

```bash
docker compose run --rm web-test pytest
```

## Mock mode vs real providers

`LLM_BACKEND=mock` (default in `.env.example`): deterministic, no network.
`LLM_BACKEND=litellm`: routes through the LiteLLM proxy container.

## Connecting external providers

API keys live **only** in environment variables (`.env`), never in the DB,
templates, or logs:

```env
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
```

Assign a player a `ModelConfig` in Admin with a `gateway_model` alias defined
in `litellm_config.yaml` (e.g. `openai-gpt4o`, `anthropic-claude-sonnet`,
`gemini-pro`). Set `LLM_BACKEND=litellm` and restart.

## Connecting host Ollama

Host Ollama is reached by containers via `host.docker.internal:11434`
(configured as `OLLAMA_BASE_URL`). The LiteLLM alias `ollama-local` maps to
`ollama/glm-5.2:cloud` by default — change it in `litellm_config.yaml` to any
model listed by `ollama list`.

Verified integration path:

```
Django (LiteLLMClient)
  → LiteLLM container (:4000)
    → host.docker.internal:11434
      → Host Ollama (glm-5.2:cloud)
```

## Context manager

The app does not send the entire campaign transcript and all world lore on
every LLM call anymore. Each player context is assembled from:

```
Campaign.system_prompt
+ relevant LoreEntry rows
+ Campaign.shared_memory
+ Player.character_prompt
+ Player.memory_summary
+ Scene.description
+ Scene.memory_summary
+ newest visible message tail
+ current GM trigger
```

`LoreEntry.scope` controls who receives an entry:

- `GLOBAL`: every player in the campaign.
- `SCENE`: only players being called in one of the entry's assigned scenes.
- `PLAYER`: only the explicitly assigned players.

Lore is packed by ascending `priority` (lower number = more important) up to
`CONTEXT_LORE_MAX_CHARS`. Visible chat history is reduced to the newest
contiguous tail up to `CONTEXT_HISTORY_MAX_CHARS`. Both limits are
provider-agnostic character budgets so the same behavior works with Ollama,
OpenAI, Anthropic, Gemini, etc.

Default values:

```env
CONTEXT_HISTORY_MAX_CHARS=40000
CONTEXT_LORE_MAX_CHARS=50000
```

Set either to `0` (or a negative value) to disable that limit.

`Campaign.shared_memory`, `Player.memory_summary`, and
`Scene.memory_summary` are compact long-term memory fields and are always
included. In this MVP they are intentionally edited by the GM in Django Admin;
automatic summarization/roll-up is a later layer. Starting a fresh campaign
does not require filling them immediately: recent history remains available
until it reaches the configured budget.

World lore is managed in Admin under **Lore entries**. Keep only foundational,
universally known facts as `GLOBAL`; route specialist/secret knowledge through
`SCENE` or `PLAYER` so models do not receive information their characters
should not know.

## Architecture

```
models
services/context_builder   # privacy + lore routing + bounded recent history
services/turn_engine        # MANUAL / ROUND / SIMULTANEOUS / TABLE + state machine
services/llm                # LLMClient: MockLLMClient | LiteLLMClient
views / templates           # thin; no business logic in views
```

Message visibility: `PUBLIC`, `PRIVATE_GM_PLAYER`, `GM_ONLY`. A single message
table with visibility rules — not separate chats.

Turn states: `PENDING / RUNNING / COMPLETED / FAILED`. Each player call has
its own `TurnExecution` state (`PENDING / RUNNING / COMPLETED / FAILED /
INVALID`). Public ROUND/SIMULTANEOUS calls use frozen context snapshots;
private GM↔player turns never advance the public round. Browser submissions use
a per-form UUID so duplicate submits are idempotent. Failed executions can be
retried without replaying successful players.

## Stop

```bash
docker compose down            # keep data
docker compose down -v         # wipe the PostgreSQL volume
```