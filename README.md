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
+ predecessor Scene.memory_summary values visible to this player
+ Scene.description
+ Scene.memory_summary
+ newest visible message tail across inherited scene history
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

## Scenes as playable sessions

A `Scene` is now effectively a playable session/thread with an explicit
participant list and optional predecessor scenes.

- `SceneParticipant` defines which campaign players are actually present.
- `PUBLIC` means public to the participants of that scene, not every player in
  the whole campaign.
- ROUND order must contain every participant of the current scene exactly once.
- A scene may inherit one or more closed predecessor scenes through
  `previous_scenes`. This supports parallel POV threads that later converge.
- When building context, a player inherits only predecessor scenes in which that
  player participated. Parallel scenes belonging to other characters remain
  invisible.
- Predecessor `memory_summary` fields are also inherited for players who
  participated in those scenes, so important history survives transcript
  trimming.

Scenes can be closed from the main scene UI. Closing is one-way in normal play:
the scene becomes read-only and no new public/private messages or turns may be
created there. A closed scene can then be used as the predecessor of a new
scene via **New scene from here**.

This makes the practical flow:

```
solo/parallel scenes
        ↓
shared scene
        ↓
close scene
        ↓
new session inheriting selected history
```

Existing scenes are migrated with every campaign player as a participant so
pre-upgrade behavior is preserved until those scenes are edited.

## Architecture

```
models
services/context_builder   # privacy + inherited scene history + bounded context
services/turn_engine        # MANUAL / ROUND / SIMULTANEOUS / TABLE + state machine
services/llm                # LLMClient: MockLLMClient | LiteLLMClient
views / templates           # thin; no business logic in views
```

Message visibility: `PUBLIC`, `PRIVATE_GM_PLAYER`, `GM_ONLY`. A single message
table with visibility rules — not separate chats. `PUBLIC` is scoped to the
participant list of the scene where the message was created.

Turn states: `PENDING / RUNNING / COMPLETED / FAILED`. Each player call has
its own `TurnExecution` state (`PENDING / RUNNING / COMPLETED / FAILED /
INVALID`). Public ROUND/SIMULTANEOUS calls use frozen context snapshots;
private GM↔player turns never advance the public round. Browser submissions use
a per-form UUID so duplicate submits are idempotent. Failed executions can be
retried without replaying successful players.

In ROUND mode an inactive player may declare `ACT_OUT_OF_TURN`. The GM can
explicitly adjudicate one of those declarations in the next public GM message
with the **SAOOT** composer control. SAOOT wraps the selected GM text in a
player-targeted marker and the UI renders it as a highlighted adjudication.
When several players declared `ACT_OUT_OF_TURN`, the GM selects which player
the marked text resolves. If the GM proceeds without a SAOOT adjudication for a
given declaration, that declaration is treated as successful as stated.

The Public and Private panes are display-only newest-first feeds; model context
continues to use canonical chronological order. Every visible GM/player message
has a Copy control that copies the author/action header plus the visible message
text; the HTTP `mraz.local` deployment falls back to the legacy browser copy
command when the secure Clipboard API is unavailable.

ROUND and MANUAL also expose a **Молчание** (Silence) control. Silence
creates a real player turn without creating a GM Message. In ROUND it runs the
normal full round roster from the current active player and advances the round
once all executions complete. In MANUAL it requires exactly one selected player
and calls only that player. Models are explicitly told that Silence means the GM
has yielded the floor and that they must continue only from already established
scene state/history rather than inventing a new GM event.

Successful public player replies expose GM correction/history controls in the
message `⋯` menu:

- **OOC** asks the GM for a private meta-comment tied to that exact public
  declaration. The same player model receives the comment privately and may
  either keep its declaration or return a full replacement. A replacement
  updates the existing public Message row in place, so it does not create a new
  action, new turn, or another ROUND advance.
- **Regen** asks only that player's model for a fresh variant from the original
  frozen turn context. The old version is kept in `MessageRevision`; a successful
  variant replaces the visible Message in place and may later be restored.
- **Versions / restore** shows every retained declaration version (original,
  Regen, OOC revision, restore) and lets the GM restore an earlier one.
- **Debug** shows the exact model alias, latency, frozen history ids, system
  prompt, request messages, raw provider response, execution state and error.
- **Memory pins** can append an edited message-derived note directly to shared
  campaign memory, scene memory, the speaking player's private memory, or a
  scene-scoped LoreEntry.

The scene toolbar also exposes **Undo last turn**. It removes the most recent
Turn and its linked messages. When a public ROUND Turn had advanced the round,
the active player position is restored to the turn's frozen active-player
snapshot.

### GM workbench

Players can have an optional fallback `ModelConfig`. Failed/invalid executions
then offer both a same-model retry and a one-off fallback retry without changing
the player's normal model assignment.

Each player card also has **Nudge**. A Nudge is a one-shot private GM
instruction snapshotted into the next execution for that player, then consumed.
It is never presented as in-fiction dialogue and retries of that same execution
retain the snapshotted Nudge.

Public ROUND provider calls run concurrently after every participant's context
has been frozen. ORM/context construction and result persistence remain ordered,
so one player's response cannot enter another player's same-round context.

The public pane has quick current-scene text/author/action filters plus a
**History search** page spanning the current scene and its predecessor lineage,
with text, author, action and visibility filters.

The GM composer has an **IC / OOC** switch. OOC mode stores meta-information in
the selected player's private context or broadcasts it to all current scene
participants. It does not create a public event or call a model immediately.

The private pane is wider, collapsible, and keeps its text composers outside the
polling fragment so typed drafts survive refreshes. Unread player messages still
show the existing dot beside the player name. By default a newly appearing
unread dot expands the private pane and selects that player's tab; **auto-open**
can be disabled and is stored in browser localStorage.

At scene close, **Close…** asks models for a draft public scene summary, open
hooks, and privacy-filtered per-player memory updates. The draft is editable and
does not change memory or close the scene until the GM presses **Apply & close**.
Direct close without summary remains available from the adjacent `⋯` menu.

GM keyboard shortcuts on the scene page:
`Ctrl+Enter` Send, `Alt+S` Silence, `Alt+R` dialogue formatting, and
`Alt+O` IC/OOC mode.

ROUND treats `ACT_OUT_OF_TURN` as an exceptional interrupt rather than a
normal alternate action. Inactive players are explicitly instructed to prefer
`PASS` unless waiting would make the intervention impossible or materially
change it. Public model responses are also guarded server-side. A normal public
`ACT` is limited to 1200 visible characters, 6 paragraphs, and 2 direct
questions. Models are explicitly told that six paragraphs is only a hard ceiling;
normal useful ACTs should usually stay around 2-3 paragraphs and must not be
padded with recaps or repeated exposition. `ACT_OUT_OF_TURN` remains deliberately stricter at 650 visible
characters, 2 paragraphs, and 1 direct question. Hidden Russian hover
translations do not count toward the visible-character limit. Violations make
that execution `INVALID`, so it can be retried without replaying successful
players.

Each scene may define a `dialogue_language` such as `French` or `Portuguese`.
The model is instructed to keep narration in Russian while emitting every
spoken sentence in the actual in-world language using:

```text
[[SPEECH]]Je vais vérifier la voiture.[[RU]]Я проверю машину.[[/SPEECH]]
```

The normal scene view shows only the original-language sentence. Hovering or
keyboard-focusing it displays the Russian translation in a tooltip. The Russian
GM interface language is not treated as the in-world spoken language.

## Manual external-chat transport

A player can use `transport = MANUAL_CHAT` instead of LiteLLM/API generation.
This is intentionally a human-in-the-loop bridge for models that are available
only through a normal web chat.

Configure the Player in Admin with:

- **transport** = `MANUAL_CHAT`
- optional **manual_chat_label** such as `ChatGPT 5.6`
- optional **manual_chat_url** pointing at the persistent external conversation
- **manual_chat_context_mode** = `FULL` or `CHAT_MEMORY`

When that player is invoked, the TurnExecution moves to
`WAITING_EXTERNAL` instead of calling LiteLLM. The player card shows the exact
packet plus **Copy prompt**, **Open chat**, and a multiline response paste box.
The pasted result then goes through the same structured-response parser,
ROUND-role validation, ACT/ACT_OUT_OF_TURN limits, bilingual speech rendering,
private_to_gm handling, Turn completion, and ROUND advancement as an API reply.
A rejected paste leaves the execution in `WAITING_EXTERNAL` with the error and
raw pasted text still visible for correction. While any manual execution is
waiting, the Turn Engine blocks starting another model turn in that scene so a
human cannot accidentally create two overlapping frozen timelines while
copying things between browser tabs.

`FULL` sends the complete authoritative application prompt and visible history
on every execution.

`CHAT_MEMORY` performs one complete **BOOTSTRAP** first. After a successful
bootstrap response is pasted back, the Player is marked synchronized. Later
executions send **DELTA** packets containing only newly visible public/private
messages since the last successfully imported external response plus current
scene/ROUND constraints, GM Silence state, one-shot Nudge, the current compact
shared/player/scene memories, and the response contract. Imported response
messages are included in the sync watermark, so the model's own previous answer
is not pointlessly echoed back on the next delta. If the last synchronized
external execution belongs to a scene outside the current predecessor lineage,
the bridge falls back to a full bootstrap instead of trusting unrelated branch
memory.

If the external conversation is replaced, cleared, or no longer remembers its
bootstrap, use **Reset chat memory** on the player card. The next execution will
send a fresh full bootstrap. Major out-of-band changes to character/world rules
should be treated the same way when you want the external chat re-seeded from
authoritative application context.

General GM **OOC / META** messages and private GM messages naturally enter the
next DELTA because they are part of that player's visible history. One-shot
**Nudge** is embedded directly in the pending manual packet and remains frozen
for that execution.

The API-only per-message **Regen** and immediate **OOC revision** controls are
hidden for manual-chat declarations rather than silently calling the player's
old LiteLLM model. Use the persistent external chat plus the normal OOC/meta
channel for now; a later browser bridge can automate the same Copy/Open/Paste
contract without changing the Turn Engine.

## Stop

```bash
docker compose down            # keep data
docker compose down -v         # wipe the PostgreSQL volume
```