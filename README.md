# MRAZ Master

MRAZ Master is a tabletop/text RPG GM interface where one human Game Master controls a campaign with multiple LLM-powered players.

The application is built around a strict turn model: LLM players never directly call other LLM players, saving a response does not start another turn, and model execution is controlled by the Turn Engine after explicit GM actions.

## Features

- One GM controlling multiple AI players.
- Public scene communication and private GM ↔ player channels.
- Multiple turn modes:
  - MANUAL
  - ROUND
  - SOFT_ROUND
  - SIMULTANEOUS
  - TABLE
- Scene-based campaign structure with:
  - participants
  - predecessor scenes
  - inherited memory
  - controlled visibility
- World lore management with scoped knowledge:
  - GLOBAL
  - SCENE
  - PLAYER
- Player memory, scene memory and campaign memory.
- Model retry and fallback handling.
- Frozen execution context for deterministic turns.
- Message revisions, regeneration and restore history.
- GM tools for nudges, corrections and memory pins.
- Mock LLM backend for development without external providers.
- LiteLLM integration for OpenAI, Anthropic, Gemini and local Ollama models.

## Technology stack

- Python 3.13
- Django 5.2 LTS
- PostgreSQL
- ASGI / Uvicorn
- LiteLLM Proxy
- Django Templates
- HTMX
- Alpine.js
- Docker Compose

## Project structure

```
rpgllm/
├── rpg/
│   ├── models.py          # campaign, scenes, players, messages and state models
│   ├── views.py           # web interface and actions
│   ├── services/
│   │   ├── context_builder # builds model context with visibility rules
│   │   ├── turn_engine     # controls turn execution
│   │   └── llm             # model provider abstraction
│   ├── templates/          # Django UI
│   └── tests/              # application tests
├── mraz/                  # Django project configuration
├── compose.yaml
├── Dockerfile
└── litellm_config.yaml
```

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

For Windows browsers add:

```
127.0.0.100 mraz.local
```

to:

```
C:\Windows\System32\drivers\etc\hosts
```

Then open:

```
http://mraz.local
```

Create an administrator account:

```bash
docker compose exec web python manage.py createsuperuser
```

Load demo data:

```bash
docker compose exec web python manage.py seed_demo
```

The demo creates a campaign, scene and several players using the mock backend.

## LLM backends

Default development mode:

```env
LLM_BACKEND=mock
```

No API keys or local models are required.

For external providers:

```env
LLM_BACKEND=litellm
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
```

Provider aliases are configured through `litellm_config.yaml`.

Host Ollama can be accessed from containers through:

```
host.docker.internal:11434
```

## Context handling

Every model call receives a controlled context assembled from:

- campaign instructions
- allowed lore entries
- shared memory
- player character data
- player memory
- visible predecessor scene memory
- current scene state
- selected recent history
- GM trigger

The system does not blindly send the entire transcript and all lore with every request.
Visibility rules are applied before context generation.

## Scene and message model

Scenes represent playable sessions. A scene can inherit previous scenes while keeping character knowledge separated.

Messages support:

- PUBLIC
- PRIVATE_GM_PLAYER
- GM_ONLY

Public messages are visible only to current scene participants.

Turn execution uses explicit states:

```
PENDING → RUNNING → COMPLETED
             ↓
           FAILED
```

Each player execution keeps its own state, allowing retries without replaying successful players.

## Development

Run tests inside Docker:

```bash
docker compose run --rm --build web-test pytest
```

## Environment

Configuration is provided through `.env`.

Important files:

- `.env.example` - available environment variables
- `compose.yaml` - local services
- `litellm_config.yaml` - model routing

## License

Private project.
