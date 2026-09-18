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

Open <http://mraz.local>.

Checks:

```bash
getent hosts mraz.local        # -> 127.0.0.100
curl -I http://mraz.local      # -> 200 OK
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

## Architecture

```
models
services/context_builder   # privacy boundary: Context(X) = PUBLIC + PRIVATE(GM,X)
services/turn_engine        # MANUAL / ROUND / SIMULTANEOUS / TABLE + state machine
services/llm                # LLMClient: MockLLMClient | LiteLLMClient
views / templates           # thin; no business logic in views
```

Message visibility: `PUBLIC`, `PRIVATE_GM_PLAYER`, `GM_ONLY`. A single message
table with visibility rules — not separate chats.

Turn states: `PENDING / RUNNING / COMPLETED / FAILED`. Double-launch is
blocked; failed turns are retried explicitly by the Master.

## Stop

```bash
docker compose down            # keep data
docker compose down -v         # wipe the PostgreSQL volume
```