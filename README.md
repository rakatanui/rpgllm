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

Create an administrator account before signing in:

```bash
docker compose exec web python manage.py createsuperuser
```

Open `http://mraz.local/`. The GM workbench redirects to `/auth/login/`.
Only active Django staff accounts can access it. The superuser above qualifies.
Game Master sessions use Django's authentication and a POST-only logout form;
HUMAN players continue using their existing scene-scoped bearer links without login.
Django Admin remains at `/admin/` and applies its own model permissions.

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


## Security and multiple Game Masters

MRAZ supports more than one Game Master with campaign-level ownership.

- Create accounts in Django Admin (or `createsuperuser` for the initial administrator).
- Grant `is_staff` to a Game Master. A regular staff account can sign into the
  workbench and edit **only campaigns that it owns**, including their scenes,
  characters, lore, turns and messages through Django Admin.
- Campaign owners are assigned in Django Admin by a superuser. Newly created
  campaigns automatically belong to the staff member who creates them.
- Superusers have global access. Shared `ModelConfig` aliases are editable
  **only** by superusers, but masters can select existing aliases.
- An existing campaign without an owner is visible only to superusers. Before
  using the new multi-master workbench, assign owners in `/admin/rpg/campaign/`.
  **No legacy campaigns are automatically assigned to an arbitrary staff user.**
- The demonstration data may be assigned with
  `python manage.py seed_demo --owner USERNAME`.

Failed login attempts are tracked by **django-axes** in PostgreSQL for both the
GM login and Django Admin. Default: 5 failed attempts, then a one-hour lockout.
Attempts are keyed by username plus client IP (not globally per IP, which\nwould lock out unrelated masters behind a shared reverse proxy). The limiter\nuses `REMOTE_ADDR`, not untrusted forwarding headers. Configure the upstream\nproxy consistently; a shared proxy IP still limits each username separately.

### HTTPS and secure sessions

Local `http://mraz.local` development is intentionally unchanged. To deploy
behind HTTPS, set these in the server's **private** `.env`:

```env
DJANGO_HTTPS_ENABLED=True
TRUST_PROXY_SSL_HEADER=True
ALLOWED_HOSTS=gm.example.org,players.example.org
CSRF_TRUSTED_ORIGINS=https://gm.example.org
PUBLIC_PLAYER_HOST=players.example.org
SECRET_KEY=<long-random-secret>
```

Important: `TRUST_PROXY_SSL_HEADER=True` is safe **only** when the reverse
proxy strips incoming `X-Forwarded-Proto` and supplies its own verified
`https` value, and Django is unreachable directly from the internet.
Otherwise leave this flag off and terminate TLS directly at the app.
`DJANGO_HTTPS_ENABLED` enables SSL redirects, secure session and CSRF cookies
and initial one-hour HSTS; start with a short HSTS duration, and enable
subdomain HSTS only after confirming every subdomain is HTTPS-only.
The internal `/health/` path is exempt from HTTPS redirect for container checks.

Run before production rollout:

```sh
python manage.py migrate
python manage.py check --deploy
python -m pytest rpg/tests/test_gm_authentication.py rpg/tests/test_production_auth_security.py
```

Bearer-link HUMAN players continue without GM accounts. Never publish the GM
workbench through `player-edge`. Brute-force protection does not replace proxy
rate limiting, monitoring, or careful secret management.

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
