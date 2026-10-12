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

The workbench's **Campaigns** home page now provides **New campaign** and
**Settings** for basic campaign metadata: name, description, global system
rules, and shared memory. New campaigns are always owned by the signed-in GM;
the form never accepts a user-supplied owner. Regular GMs can edit campaigns they own **or are explicitly invited to co-master**.
Superusers can edit unassigned or other-owned campaigns. Owner transfers and
advanced lore editing remain in Django Admin.

The **Campaigns → Open** workbench lets the owner, invited co-masters and
superusers create and edit characters and scenes without leaving the application. Character
settings include player type (human, API model, or external chat), model
selection, character sheet, prompts, and private memory. Scene settings include
its title, description, dialogue language, turn mode, memory, and participant
roster. All selections are restricted to the current campaign.

**Shared campaigns (co-Game Masters):** Each campaign has one responsible
owner. The owner's **Game Masters** page lets them grant and revoke access to
existing active staff accounts by username. Additional Game Masters have the
same in-campaign workbench access as the owner: scene actions and private
channels, campaign/player/scene editing, model GM, lore, and the private
campaign timeline. Membership is campaign-specific and does not change
ownership. Owners and superusers alone can manage membership or archive,
restore, trash or purge a campaign; irreversible purging remains superuser-only.
New account creation and invitations by email are not included.
An existing authenticated session loses access immediately upon revocation,
and disabled/nonstaff accounts cannot use an old membership. Archived teams
are visible but read-only. The recycle bin is owner-only.
Django Admin uses the same membership checks for read/write and related
selectors; a co-master cannot transfer campaign ownership.

Scenes can inherit **multiple closed predecessors** in the campaign editor. The
**Continue…** action on a closed scene opens the scene-creation form with its
previous-scene link, cast, language, and turn mode preselected. You can add
additional closed parallel branches. The previous messages and summaries are
**referenced, not copied** into the new scene, and the new scene has independent
access tokens and memory. Player context includes only predecessor scenes they
actually participated in, excludes all GM-only content, and limits private
messages to their original recipient. This applies even when separate branches
converge. The GM can see the combined history. Editing links is possible only
before play, and never after a successor already depends on the scene. Foreign,
open, self-referencing or cyclic predecessor selections are rejected.

**Campaign archival, recovery and deletion:** The campaign list separates active
campaigns, a read-only archive and a recycle bin. The owner can archive an
idle campaign and later restore it without changing scenes, historical
messages, player tokens, portraits, NPC plans or the story clock. Archived
campaigns remain readable to the GM; all GM write endpoints and public player
bearer links are disabled until restoration. Deleting a campaign normally means
**moving it into the recycle bin**, not destroying data. Owners can recover
these records: a previously archived campaign returns to the archive rather
than resuming gameplay. These operations refuse to proceed while turns or
LLM executions are pending, running or waiting for player/external input.
Destructive actions require typing the exact campaign name and use POST/CSRF.

Only a **superuser** can deliberately erase a campaign forever, and only after
the campaign has first been moved to the recycle bin. This destroys related
database objects and schedules cleanup of image files after successful commit,
skipping files still referenced by other campaigns. Permanent erasure cannot
be reversed; make a tested off-server backup first. There is no automatic
recycle-bin expiry or timed purge. Archived and recycled campaigns can still
occupy storage until explicitly erased.

The **Campaign timeline** is a GM-only Outlook-style story calendar, available
from the campaign workspace and the GM scene toolbar. It has month, week and
day views. Its manually controlled **campaign date and time** are independent
of the real-world server clock. Advancing the clock does not execute scheduled
plans. Each private calendar entry can be an **NPC plan** or a **story marker**,
with an in-world start date/time, optional end date/time, title, NPC/faction,
context, and an expected outcome **if players do not interfere**. The GM can
later record that it was changed, cancelled, or happened, preserving the
original expected outcome and documenting actual consequences. These GM-only
plans are not sent to player pages or LLM/player context; there is no automated
event scheduler or world-simulation engine in this feature.

Participant additions and removals are allowed **only before play has begun**
(no turns, scene messages, or GM executions). Retained scene participation
records are preserved intact so existing HUMAN bearer links, revocation settings,
and selected appearances do not change. When the roster changes, the previously
confirmed ROUND order is cleared and must be confirmed again in the live scene.
Closed scenes cannot be edited through the campaign workbench. This release
intentionally does not implement deletion, duplication, or moving characters
between campaigns; those operations have separate data-integrity requirements.

- Create accounts in Django Admin (or `createsuperuser` for the initial administrator).
- Grant `is_staff` to a Game Master. Regular staff can sign into the workbench
  and edit campaigns they **own or have been invited to co-master**, including
  scenes, characters, lore, turns and messages through Django Admin.
- Campaign owners are assigned in Django Admin by a superuser. Newly created
  campaigns automatically belong to the staff member who creates them.
- Superusers have global access. Shared `ModelConfig` aliases are editable
  **only** by superusers, but masters can select existing aliases.
- An existing campaign without an owner is visible only to superusers unless
  an administrator explicitly assigns a co-master. Prefer assigning an owner
  via `/admin/rpg/campaign/` for proper lifecycle and membership management.
  **No legacy campaign is automatically assigned to a staff account.**
- The demonstration data may be assigned with
  `python manage.py seed_demo --owner USERNAME`.

Failed login attempts are tracked by **django-axes** in PostgreSQL for both the
GM login and Django Admin. Default: 5 failed attempts, then a one-hour lockout.
Attempts are keyed by username plus client IP (not globally per IP, which
would lock out unrelated masters behind a shared reverse proxy). The limiter
uses `REMOTE_ADDR`, not untrusted forwarding headers. Configure the upstream
proxy consistently; a shared proxy IP still limits each username separately.

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

## Public player access hardening

HUMAN players authenticate with a scene-specific UUID bearer link under `/play/`.
The token is **equivalent to a password** for that scene: never publish it in
tickets, screenshots, analytics, monitoring traces or public chat messages.
The GM can revoke or regenerate it at any time. Player routes always resolve
the scene and player from the token, then check related object IDs. A token
from one scene cannot reveal another player's private messages or future scenes.

**Token safety.** All `/play/` responses (including failures, redirects,
images and QR codes) use `Cache-Control: private, no-store`,
`Referrer-Policy: no-referrer` and `X-Content-Type-Options: nosniff`.
Tokenized browser POSTs reject foreign `Origin` and cross-site
`Sec-Fetch-Site`; the bearer URL itself remains sufficient without a CSRF
cookie. Django application logs redact the UUIDs. The Docker Uvicorn startup
disables access logging; the public Nginx player-edge also suppresses request
logs for `/play/`. Other reverse proxies, CDN analytics and error-reporting
services must **also** avoid recording full URLs with bearer credentials.

**Abuse limits.** Writes and QR generation use database-backed fixed-window
counters shared across Django workers; counters store SHA-256 token/action
bucket digests, not raw tokens. Defaults per bearer token:

- Human turn: 12 submissions/minute
- OOC: 10 messages/minute (each message limited to 8,000 characters)
- Appearance change: 20 requests/minute
- Upload or removal: 3 requests/5 minutes
- QR generation: 30 requests/minute

Gameplay submissions cannot exceed 16,000 total characters between public and
private text; history search and pagination reject pathological input.
The read-only `/play/` status and fragment polling remains unlimited by
these database quotas. For protection against invalid-token floods and GET
traffic, configure rate limits at the public reverse proxy/CDN, with a
**trusted** real-client-IP source; do not key a limit against the cloudflared
container IP shared by every player.

**Portrait uploads.** Accept JPEG, PNG and WebP up to 5 MB / 12 megapixels.
The server decodes and re-encodes approved images to strip EXIF and embedded
metadata; it does not save the original upload. Read-only closed scenes cannot
upload or remove portraits. Media remains behind token-gated Django routes,
not a public `/media/` mapping.

Database rate counter rows expire logically after their window. Schedule
maintenance **daily** as part of production operations:

```sh
docker compose exec web python manage.py cleanup_player_rate_buckets
```

After deploying this branch, apply migrations before serving player requests:

```sh
docker compose exec web python manage.py migrate --noinput
```

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
