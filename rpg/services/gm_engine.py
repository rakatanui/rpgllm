"""Model Game Master orchestration.

A GM execution produces a draft first. Publishing that draft is the only point
that mutates scene canon or opens a player turn. Nothing in this module
automatically schedules another GM execution after players respond.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from rpg.models import (
    AuthorType,
    GameMasterAction,
    GameMasterConfig,
    GameMasterExecution,
    GameMasterExecutionState,
    GameMasterTransport,
    ManualChatContextMode,
    Message,
    Player,
    PlayerTransport,
    Scene,
    TurnMode,
    TurnState,
    Visibility,
)
from rpg.services import turn_engine
from rpg.services.context_builder import get_scene_lineage
from rpg.services.gm_context import (
    build_gm_authoritative_fact_corpus,
    build_gm_context,
    build_gm_knowledge_retrieval,
    gm_lookup_fillable_scopes,
)
from rpg.services.llm import get_llm_client


@dataclass
class GameMasterResponse:
    raw_text: str
    action: str
    public: str
    private: list[dict]
    turn_targets: list[int]
    scene_transition: dict | None = None


ACTIVE_GM_STATES = {
    GameMasterExecutionState.PENDING,
    GameMasterExecutionState.RUNNING,
    GameMasterExecutionState.WAITING_EXTERNAL,
    GameMasterExecutionState.DRAFT,
}


_PREEXISTING_EXACT_FACT_PATTERNS = (
    re.compile(
        r"\b(?:ul\.?|al\.?|aleja|plac|pl\.?|street|st\.?|road|rd\.?|"
        r"улиц\w*|ул\.?|проспект\w*|пр-т|переул\w*|пер\.?)\s+"
        r"[A-Za-zА-Яа-яЁёÀ-ž'’.-]+(?:\s+[A-Za-zА-Яа-яЁёÀ-ž'’.-]+){0,5}\s+"
        r"\d+[A-Za-zА-Яа-я]?(?:[/-]\d+)?",
        flags=re.IGNORECASE,
    ),
    re.compile(r"(?<!\w)\+?\d[\d\s()\-]{7,}\d(?!\w)"),
    re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b"),
    re.compile(
        r"\b(?:парол\w*|код\w*|номер\s+дела|регистрацион\w*\s+номер|"
        r"case\s*(?:no\.?|number)?)\s*[:#№-]?\s*"
        r"[A-ZА-Я0-9][A-ZА-Я0-9._/-]{3,}\b",
        flags=re.IGNORECASE,
    ),
)


def _normalize_fact_literal(text: str) -> str:
    normalized = (text or "").casefold().replace("ё", "е")
    normalized = re.sub(r"[\s,;:()]+", " ", normalized)
    return normalized.strip(" .")


def _validate_existing_source_exact_literals(
    response: GameMasterResponse,
    *,
    scene: Scene,
) -> None:
    """Reject machine-detectable exact facts invented during an existing-source lookup.

    Prompt rules cover the general semantic case. This conservative hard guard catches
    the most damaging structured literals (addresses, phones, dates, codes) before they
    cross the publication/canon boundary.
    """
    retrieval = build_gm_knowledge_retrieval(scene=scene)
    if not retrieval:
        return

    fillable_scopes = gm_lookup_fillable_scopes(scene=scene)
    explicit_fillable = any(
        not scope.startswith("AUTO OPERATIONAL SOURCE:")
        for scope in fillable_scopes
    )
    auto_operational = any(
        scope.startswith("AUTO OPERATIONAL SOURCE:")
        for scope in fillable_scopes
    )

    # Explicit author-controlled GM_FILLABLE is a deliberate broad exception for
    # its matching subject. Automatic operational filling is narrower: it may
    # establish routine dated/current working data, but it must not accidentally
    # authorize unrelated addresses, phones, passwords, case numbers, etc.
    if explicit_fillable:
        return

    corpus = _normalize_fact_literal(build_gm_authoritative_fact_corpus(scene=scene))
    padded_corpus = f" {corpus} "
    response_text = "\n".join(
        [
            response.public,
            *[
                str(item.get("content", "") or "")
                for item in response.private
            ],
        ]
    )
    for pattern_index, pattern in enumerate(_PREEXISTING_EXACT_FACT_PATTERNS):
        if auto_operational and pattern_index == 2:
            # A date printed on a current weather/watch/operational sheet is routine
            # metadata and may be established with the rest of that source.
            continue
        for match in pattern.finditer(response_text):
            literal = match.group(0).strip()
            normalized = _normalize_fact_literal(literal)
            if normalized and f" {normalized} " not in padded_corpus:
                raise ValidationError(
                    "GM response introduced an unsupported exact datum while resolving "
                    f"an existing-source lookup: {literal!r}. Add it to authoritative "
                    "lore/memory/history first or answer that the datum is unavailable."
                )


def gm_response_contract() -> str:
    return (
        'Return ONLY one JSON object: '
        '{"action":"TURN|NARRATE|WAIT","public":"...",'
        '"private":[{"player_id":123,"content":"..."}],"turn_targets":[123],'
        '"scene_transition":null}. '
        "TURN publishes the GM beat and opens a normal player turn. NARRATE publishes "
        "without calling players. WAIT publishes nothing. Player references must use the "
        "numeric player_id values from the application context. scene_transition must be "
        "null unless movement or a materially changed situation makes the live Scene label/state "
        'false; then use {"name":"New scene label","description":"concise current-state description",'
        '"memory":"compact durable scene memory after the transition"}. description and memory are "
        "required on a transition so stale pre-transition state is not kept as CURRENT SCENE."
    )

def gm_execution_request(scene: Scene) -> str:
    text = (
        "## EXECUTION REQUEST\n"
        "The Game Master has been explicitly invoked to produce the next immediate GM beat now. "
        "Advance the fiction by one immediate beat from the authoritative current state. "
        "The absence of new canon messages since the previous synchronization does not by itself "
        "justify WAIT. Use TURN when this beat should be followed by player action, NARRATE when "
        "the beat should enter canon without immediately opening a player turn, and WAIT only when "
        "the established fiction specifically requires the GM to take no action at this moment.\n\n"
        "AUTHORITATIVE-SOURCE GUARD: distinguish protected pre-existing sources from routine current "
        "operational data. For READ_EXISTING_SOURCE and RECALL_EXISTING_FACT involving dossiers, "
        "correspondence, archives, memories, logs, passwords, evidence, hidden cargo, prior events, or "
        "other plot-significant history, never invent missing content unless authoritative context supports "
        "it. Exact addresses, names, phone/registration numbers, dates, message contents, passwords, codes, "
        "case numbers, prior links/events, and protected existing-object properties remain guarded. "
        "A matching author-controlled [[GM_FILLABLE]]...[[/GM_FILLABLE]] scope explicitly permits filling "
        "its missing details. Routine USE_OPERATIONAL_DATA may also receive an automatic narrow fillable "
        "scope for current weather, watch sheets, ordinary schedules/manifests, instrument-derived values, "
        "navigation inputs, and similar non-mystery working data. PROFESSIONAL_ACTION by itself is not a "
        "source lookup and must not be blocked. Any invented fillable detail becomes fixed canon when "
        "published and may not be freely changed later.\n\n"
        "SCENE TRANSITION LIFECYCLE: when scene_transition is necessary, do not change only the label. "
        "Return a concise new current-state description and a durable memory summary in the transition "
        "object. The memory should carry forward relevant durable facts from the prior state while replacing "
        "stale current-location/current-situation wording. The old scene description may remain in history, "
        "but it must not continue to describe the live CURRENT SCENE after the transition.\n\n"
        "PLAYER DECLARATION SEMANTICS: a PLAYER message is authoritative for that character's "
        "voluntary action, speech, thought/intent, perception, estimate, or report. A player's claim "
        "about NPC behavior, external consequences, exact measurements, or world state is not objective "
        "GM confirmation unless earlier canon already establishes it. Preserve assessments as assessments "
        "until the GM confirms the world-state result.\n\n"
        "PACING: do not simulate every minute of stable repetitive work. If no meaningful decision or "
        "change is pending, compress routine time to the next natural change. Meaningful changes include "
        "new information/tasks/constraints/opportunities, changed conditions, problems, NPC decisions, "
        "relationship shifts, conflicts of interest, messages, faults, important objects, or significant "
        "results. Do not manufacture complications because a scene is calm, and do not skip across a "
        "moment where the player could make an important choice. NPCs may naturally end conversations "
        "and return to their own duties when their reason to keep talking is exhausted.\n\n"
        "PROFESSIONAL COMPETENCE: once a character has demonstrated baseline competence, do not keep "
        "forcing elementary training exchanges. Let routine professional work proceed independently; "
        "senior NPCs should supervise outcomes and intervene for mistakes, unusual conditions, or "
        "vehicle-specific concerns. Technical detail should create decisions or texture, not repetitive "
        "textbook loops.\n\n"
        "NPC CAUSALITY: do not create suspicious, dramatic, or plot-significant NPC behavior merely "
        "because the player is nearby or because a GM beat is required. Such behavior needs support "
        "in NPC goals/knowledge, scene state, an ongoing event, or a direct consequence. Ordinary "
        "background life remains allowed. Do NOT use this constraint as a reason to prefer WAIT when "
        "there is an immediate observable consequence or a natural beat the player can react to."
    )

    participants = list(
        scene.scene_participants.select_related("player").order_by("order", "pk")
    )
    if scene.mode == TurnMode.ROUND:
        order = list(scene.round_order or [])
        active_id = None
        if order and 0 <= scene.active_player_index < len(order):
            active_id = order[scene.active_player_index]
        active_participation = next(
            (
                item
                for item in participants
                if item.player_id == active_id
            ),
            None,
        )
        if active_participation is not None:
            active_player = active_participation.player
            text += (
                "\n\nROUND CONTROL: the active player for the next player turn is "
                f"{active_player.display_name} (player_id={active_player.pk}). "
                "Frame the immediate beat so this active player has a clear opportunity "
                "to react or act. Do not make an inactive participant the sole required "
                "responder unless the fiction specifically requires an urgent out-of-turn "
                "intervention. Because this scene is ROUND, TURN must always return "
                "turn_targets=[]; the application owns round order and rejects explicit targets."
            )
    if (
        scene.mode == TurnMode.MANUAL
        and len(participants) == 1
        and participants[0].player.transport == PlayerTransport.HUMAN
    ):
        player_id = participants[0].player_id
        text += (
            f" This MANUAL scene has exactly one HUMAN participant, player_id={player_id}. "
            "If your beat introduces any event, NPC action or dialogue, environmental change, "
            "clue, sensory cue, or consequence that this player could immediately react to, "
            f"you must use TURN with turn_targets=[{player_id}]. "
            "Use NARRATE only when you intentionally want no immediate player response."
        )
    retrieval = build_gm_knowledge_retrieval(scene=scene)
    if retrieval:
        text += "\n\n" + retrieval
    return text


def _normalize_model_gm_response(
    response: GameMasterResponse,
    *,
    scene: Scene,
) -> GameMasterResponse:
    if response.action != GameMasterAction.NARRATE or not response.public:
        return response
    if scene.mode != TurnMode.MANUAL:
        return response

    participants = list(
        scene.scene_participants.select_related("player").order_by("order", "pk")
    )
    if (
        len(participants) != 1
        or participants[0].player.transport != PlayerTransport.HUMAN
    ):
        return response

    normalized = GameMasterResponse(
        raw_text=response.raw_text,
        action=GameMasterAction.TURN,
        public=response.public,
        private=response.private,
        turn_targets=[participants[0].player_id],
        scene_transition=response.scene_transition,
    )
    _validate_gm_response(normalized, scene=scene)
    return normalized


def get_active_gm_execution(scene: Scene) -> GameMasterExecution | None:
    return (
        GameMasterExecution.objects.filter(
            scene=scene,
            state__in=ACTIVE_GM_STATES,
        )
        .select_related("config")
        .order_by("-created_at", "-pk")
        .first()
    )


def get_latest_gm_execution(scene: Scene) -> GameMasterExecution | None:
    return (
        GameMasterExecution.objects.filter(scene=scene)
        .select_related("config", "published_turn")
        .order_by("-created_at", "-pk")
        .first()
    )


def maybe_start_auto_gm(*, scene: Scene) -> GameMasterExecution | None:
    """Start the next GM beat after a completed public HUMAN turn when enabled.

    This helper is intentionally best-effort: a player's accepted move must stay
    accepted even if the automatic GM continuation cannot start because another
    turn/execution appeared concurrently or the scene/configuration changed.
    """
    scene = Scene.objects.select_related("campaign").get(pk=scene.pk)
    config = (
        GameMasterConfig.objects.filter(campaign=scene.campaign)
        .select_related("model_config", "fallback_model_config")
        .first()
    )
    if (
        scene.is_closed
        or config is None
        or not config.enabled
        or not config.auto_continue
        or config.review_before_publish
    ):
        return None
    if (
        config.transport == GameMasterTransport.MANUAL_CHAT
        and not (config.manual_chat_url or "").strip()
    ):
        return None

    latest_public_turn = (
        scene.turns.filter(is_private=False)
        .prefetch_related("executions")
        .order_by("-created_at", "-pk")
        .first()
    )
    if latest_public_turn is not None and latest_public_turn.state == TurnState.COMPLETED:
        actions = [
            (execution.action_type or "").strip().upper()
            for execution in latest_public_turn.executions.all()
        ]
        if actions and all(action == "PASS" for action in actions):
            # Safety brake: an all-PASS public turn contains no player action for
            # autoplay to react to. Starting another automatic GM beat here can
            # create a self-sustaining GM -> PASS -> GM loop.
            return None

    if scene.turns.filter(state=TurnState.RUNNING).exists():
        return None
    if get_active_gm_execution(scene) is not None:
        return None

    try:
        return start_gm_execution(scene=scene)
    except ValidationError:
        return None


def start_gm_execution(*, scene: Scene) -> GameMasterExecution:
    scene = Scene.objects.select_related("campaign").get(pk=scene.pk)
    if scene.is_closed:
        raise ValidationError("Cannot run a model GM in a closed scene.")

    config = (
        GameMasterConfig.objects.filter(campaign=scene.campaign)
        .select_related("model_config", "fallback_model_config")
        .first()
    )
    if config is None or not config.enabled:
        raise ValidationError("Model Game Master is not enabled for this campaign.")

    if scene.turns.filter(state=TurnState.RUNNING).exists():
        raise ValidationError(
            "A player turn is still running. Finish it before asking the model GM."
        )
    if get_active_gm_execution(scene) is not None:
        raise ValidationError(
            "This scene already has an unfinished model-GM execution. "
            "Publish, discard, or complete it first."
        )

    execution = GameMasterExecution.objects.create(
        scene=scene,
        config=config,
        state=GameMasterExecutionState.PENDING,
        transport=config.transport,
        external_context_mode=(
            config.manual_chat_context_mode
            if config.transport == GameMasterTransport.MANUAL_CHAT
            else ""
        ),
        external_chat_label=(
            config.manual_chat_label
            if config.transport == GameMasterTransport.MANUAL_CHAT
            else ""
        ),
        external_chat_url=(
            config.manual_chat_url
            if config.transport == GameMasterTransport.MANUAL_CHAT
            else ""
        ),
    )

    context = build_gm_context(scene=scene, config=config)
    GameMasterExecution.objects.filter(pk=execution.pk).update(
        context_message_ids=context.message_ids,
        system_prompt_snapshot=context.system_prompt,
        request_messages=context.messages,
    )
    execution.context_message_ids = context.message_ids
    execution.system_prompt_snapshot = context.system_prompt
    execution.request_messages = context.messages

    if config.transport == GameMasterTransport.MANUAL_CHAT:
        _prepare_external_execution(
            execution=execution,
            config=config,
            system_prompt=context.system_prompt,
            messages=context.messages,
        )
    else:
        _run_provider_execution(
            execution=execution,
            config=config,
            system_prompt=context.system_prompt,
            messages=context.messages,
        )

    execution.refresh_from_db()
    if (
        execution.state == GameMasterExecutionState.DRAFT
        and not config.review_before_publish
    ):
        publish_gm_execution(execution=execution)
        execution.refresh_from_db()
    return execution


def submit_external_gm_response(
    *,
    execution: GameMasterExecution,
    raw_text: str,
) -> GameMasterExecution:
    raw = (raw_text or "").strip()
    with transaction.atomic():
        execution = (
            GameMasterExecution.objects
            .select_for_update()
            .get(pk=execution.pk)
        )

        execution.scene = (
            Scene.objects
            .select_related("campaign")
            .get(pk=execution.scene_id)
        )

        if execution.config_id:
            execution.config = (
                GameMasterConfig.objects
                .get(pk=execution.config_id)
            )
        if execution.scene.is_closed:
            raise ValidationError("Cannot import a GM response into a closed scene.")
        if execution.transport != GameMasterTransport.MANUAL_CHAT:
            raise ValidationError("GM execution is not using manual external chat.")
        if execution.state != GameMasterExecutionState.WAITING_EXTERNAL:
            raise ValidationError("GM execution is not waiting for an external response.")
        if not raw:
            execution.error = "External GM response cannot be empty."
            execution.raw_response = ""
            execution.save(update_fields=["error", "raw_response", "updated_at"])
            raise ValidationError(execution.error)

        try:
            response = parse_gm_response(raw, scene=execution.scene)
            response = _normalize_model_gm_response(
                response,
                scene=execution.scene,
            )
        except Exception as exc:
            execution.error = str(exc)
            execution.raw_response = raw
            execution.save(update_fields=["error", "raw_response", "updated_at"])
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError(f"External GM response rejected: {exc}") from exc

        _apply_response_to_execution(execution, response)
        execution.external_synced_message_ids = list(execution.context_message_ids or [])
        execution.save(
            update_fields=[
                "state",
                "action",
                "public_draft",
                "private_drafts",
                "turn_targets",
                "scene_transition",
                "error",
                "raw_response",
                "external_synced_message_ids",
                "updated_at",
            ]
        )

        config = execution.config
        if (
            config is not None
            and execution.external_context_mode == ManualChatContextMode.CHAT_MEMORY
            and execution.external_is_bootstrap
        ):
            GameMasterConfig.objects.filter(pk=config.pk).update(
                manual_chat_initialized=True
            )
            config.manual_chat_initialized = True

    execution.refresh_from_db()
    if execution.config and not execution.config.review_before_publish:
        publish_gm_execution(execution=execution)
        execution.refresh_from_db()
    return execution


def publish_gm_execution(
    *,
    execution: GameMasterExecution,
    action: str | None = None,
    public_text: str | None = None,
    private_by_player: dict[int, str] | None = None,
    turn_target_ids: list[int] | None = None,
) -> GameMasterExecution:
    execution = (
        GameMasterExecution.objects.select_related("scene__campaign", "config")
        .get(pk=execution.pk)
    )
    scene = execution.scene
    if scene.is_closed:
        raise ValidationError("Cannot publish a GM draft into a closed scene.")
    if execution.state != GameMasterExecutionState.DRAFT:
        raise ValidationError("Only a ready GM draft can be published.")
    if scene.turns.filter(state=TurnState.RUNNING).exists():
        raise ValidationError(
            "A player turn started after this draft was generated. Finish it before publishing."
        )

    response = _response_from_execution(
        execution,
        action=action,
        public_text=public_text,
        private_by_player=private_by_player,
        turn_target_ids=turn_target_ids,
    )
    _validate_gm_response(response, scene=scene)

    with transaction.atomic():
        locked = GameMasterExecution.objects.select_for_update().get(pk=execution.pk)
        if locked.state != GameMasterExecutionState.DRAFT:
            raise ValidationError("GM draft state changed before publication.")
        locked.state = GameMasterExecutionState.RUNNING
        locked.action = response.action
        locked.public_draft = response.public
        locked.private_drafts = response.private
        locked.turn_targets = response.turn_targets
        locked.scene_transition = response.scene_transition or {}
        locked.error = ""
        locked.save(
            update_fields=[
                "state",
                "action",
                "public_draft",
                "private_drafts",
                "turn_targets",
                "scene_transition",
                "error",
                "updated_at",
            ]
        )

    published_turn = None
    original_scene_state = {
        "name": scene.name,
        "description": scene.description,
        "memory_summary": scene.memory_summary,
    }
    transitioned = False
    try:
        private_map = {
            int(item["player_id"]): item["content"]
            for item in response.private
            if item.get("content", "").strip()
        }

        if response.scene_transition:
            new_name = response.scene_transition["name"]
            new_description = response.scene_transition["description"]
            new_memory = response.scene_transition["memory"]
            Scene.objects.filter(pk=scene.pk).update(
                name=new_name,
                description=new_description,
                memory_summary=new_memory,
                updated_at=timezone.now(),
            )
            scene.name = new_name
            scene.description = new_description
            scene.memory_summary = new_memory
            transitioned = True

        if response.action == GameMasterAction.TURN:
            selected_players = _players_from_ids(scene, response.turn_targets)
            result = turn_engine.start_turn(
                scene=scene,
                gm_message_text=response.public,
                selected_players=selected_players or None,
                private_gm_messages=private_map,
            )
            published_turn = result.turn

        elif response.action == GameMasterAction.NARRATE:
            with transaction.atomic():
                if response.public:
                    Message.objects.create(
                        campaign=scene.campaign,
                        scene=scene,
                        author_type=AuthorType.GM,
                        content=response.public,
                        visibility=Visibility.PUBLIC,
                    )
                players = {player.pk: player for player in _scene_players(scene)}
                for player_id, content in private_map.items():
                    Message.objects.create(
                        campaign=scene.campaign,
                        scene=scene,
                        author_type=AuthorType.GM,
                        content=content,
                        visibility=Visibility.PRIVATE_GM_PLAYER,
                        private_player=players[player_id],
                    )

        elif response.action != GameMasterAction.WAIT:
            raise ValidationError(f"Unsupported GM action: {response.action}")

    except Exception as exc:
        if transitioned:
            Scene.objects.filter(pk=scene.pk).update(
                name=original_scene_state["name"],
                description=original_scene_state["description"],
                memory_summary=original_scene_state["memory_summary"],
                updated_at=timezone.now(),
            )
            scene.name = original_scene_state["name"]
            scene.description = original_scene_state["description"]
            scene.memory_summary = original_scene_state["memory_summary"]
        GameMasterExecution.objects.filter(pk=execution.pk).update(
            state=GameMasterExecutionState.DRAFT,
            error=str(exc),
        )
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"Could not publish GM draft: {exc}") from exc

    GameMasterExecution.objects.filter(pk=execution.pk).update(
        state=GameMasterExecutionState.PUBLISHED,
        published_turn=published_turn,
        published_at=timezone.now(),
        error="",
    )
    execution.refresh_from_db()
    return execution


def discard_gm_execution(*, execution: GameMasterExecution) -> GameMasterExecution:
    with transaction.atomic():
        execution = (
            GameMasterExecution.objects.select_for_update()
            .get(pk=execution.pk)
        )
        if execution.state not in ACTIVE_GM_STATES:
            raise ValidationError("This GM execution is already finished.")
        execution.state = GameMasterExecutionState.DISCARDED
        execution.save(update_fields=["state", "updated_at"])

        if (
            execution.config_id is not None
            and execution.transport == GameMasterTransport.MANUAL_CHAT
            and execution.external_context_mode == ManualChatContextMode.CHAT_MEMORY
        ):
            GameMasterConfig.objects.filter(pk=execution.config_id).update(
                manual_chat_initialized=False
            )
    return execution


def reset_gm_manual_chat_memory(*, config: GameMasterConfig) -> None:
    GameMasterConfig.objects.filter(pk=config.pk).update(
        manual_chat_initialized=False
    )


def parse_gm_response(raw_text: str, *, scene: Scene) -> GameMasterResponse:
    raw = (raw_text or "").strip()
    if not raw:
        raise ValidationError("Model GM returned an empty response.")

    obj = _decode_json_object(raw)
    if obj is None:
        raise ValidationError("Model GM must return one JSON object.")

    action = str(obj.get("action", "")).strip().upper()
    public = str(obj.get("public", "") or "").strip()

    private_raw = obj.get("private", [])
    if private_raw is None:
        private_raw = []
    if not isinstance(private_raw, list):
        raise ValidationError('GM field "private" must be a list.')

    private: list[dict] = []
    seen_private: set[int] = set()
    for item in private_raw:
        if not isinstance(item, dict):
            raise ValidationError('Each item in GM field "private" must be an object.')
        try:
            player_id = int(item.get("player_id"))
        except (TypeError, ValueError):
            raise ValidationError("Private GM message has an invalid player_id.")
        content = str(item.get("content", "") or "").strip()
        if not content:
            continue
        if player_id in seen_private:
            raise ValidationError(
                f"Duplicate private GM entry for player_id={player_id}."
            )
        seen_private.add(player_id)
        private.append({"player_id": player_id, "content": content})

    scene_transition_raw = obj.get("scene_transition", None)
    scene_transition = None
    if scene_transition_raw not in (None, {}):
        if not isinstance(scene_transition_raw, dict):
            raise ValidationError('GM field "scene_transition" must be null or an object.')
        name = str(scene_transition_raw.get("name", "") or "").strip()
        description = str(scene_transition_raw.get("description", "") or "").strip()
        memory = str(scene_transition_raw.get("memory", "") or "").strip()
        if not name:
            raise ValidationError('GM scene_transition requires a non-empty "name".')
        if len(name) > 200:
            raise ValidationError("GM scene_transition name is too long.")
        if not description:
            raise ValidationError(
                'GM scene_transition requires a concise non-empty "description" of the new current state.'
            )
        if not memory:
            raise ValidationError(
                'GM scene_transition requires a non-empty "memory" summary for durable scene state.'
            )
        if name != scene.name or description != scene.description or memory != scene.memory_summary:
            scene_transition = {
                "name": name,
                "description": description,
                "memory": memory,
            }

    targets_raw = obj.get("turn_targets", [])
    if targets_raw is None:
        targets_raw = []
    if not isinstance(targets_raw, list):
        raise ValidationError('GM field "turn_targets" must be a list.')
    try:
        targets = [int(value) for value in targets_raw]
    except (TypeError, ValueError):
        raise ValidationError("GM turn_targets must contain integer Player IDs.")
    if len(targets) != len(set(targets)):
        raise ValidationError("GM turn_targets contains duplicate Player IDs.")

    response = GameMasterResponse(
        raw_text=raw_text,
        action=action,
        public=public,
        private=private,
        turn_targets=targets,
        scene_transition=scene_transition,
    )
    _validate_gm_response(response, scene=scene)
    _validate_existing_source_exact_literals(response, scene=scene)
    return response


def _validate_gm_response(response: GameMasterResponse, *, scene: Scene) -> None:
    if response.scene_transition:
        transition_name = str(response.scene_transition.get("name", "") or "").strip()
        transition_description = str(
            response.scene_transition.get("description", "") or ""
        ).strip()
        transition_memory = str(
            response.scene_transition.get("memory", "") or ""
        ).strip()
        if not transition_name:
            raise ValidationError('GM scene_transition requires a non-empty "name".')
        if len(transition_name) > 200:
            raise ValidationError("GM scene_transition name is too long.")
        if not transition_description:
            raise ValidationError(
                'GM scene_transition requires a concise non-empty "description" of the new current state.'
            )
        if not transition_memory:
            raise ValidationError(
                'GM scene_transition requires a non-empty "memory" summary for durable scene state.'
            )

    if response.action not in {
        GameMasterAction.TURN,
        GameMasterAction.NARRATE,
        GameMasterAction.WAIT,
    }:
        raise ValidationError("GM action must be TURN, NARRATE, or WAIT.")

    participant_ids = set(
        scene.scene_participants.values_list("player_id", flat=True)
    )
    referenced_ids = {
        int(item["player_id"]) for item in response.private
    } | set(response.turn_targets)
    foreign = sorted(referenced_ids - participant_ids)
    if foreign:
        raise ValidationError(
            f"GM response references players outside this scene: {foreign}"
        )

    if response.action == GameMasterAction.WAIT:
        if (
            response.public
            or response.private
            or response.turn_targets
            or response.scene_transition
        ):
            raise ValidationError(
                "WAIT must have empty public, private, turn_targets, and scene_transition fields."
            )
        return

    if response.action == GameMasterAction.NARRATE:
        if response.turn_targets:
            raise ValidationError("NARRATE must not contain turn_targets.")
        if not response.public and not response.private:
            raise ValidationError("NARRATE must publish at least one message.")
        return

    if not response.public:
        raise ValidationError("TURN requires a non-empty public GM beat.")

    if scene.mode in (TurnMode.MANUAL, TurnMode.SOFT_ROUND) and not response.turn_targets:
        raise ValidationError(
            f"TURN in {scene.mode} mode requires at least one turn target."
        )
    if scene.mode == TurnMode.ROUND and response.turn_targets:
        raise ValidationError(
            "TURN in ROUND mode must leave turn_targets empty; the engine owns round order."
        )


def _response_from_execution(
    execution: GameMasterExecution,
    *,
    action: str | None,
    public_text: str | None,
    private_by_player: dict[int, str] | None,
    turn_target_ids: list[int] | None,
) -> GameMasterResponse:
    private = execution.private_drafts
    if private_by_player is not None:
        private = [
            {"player_id": int(player_id), "content": (content or "").strip()}
            for player_id, content in private_by_player.items()
            if (content or "").strip()
        ]
    return GameMasterResponse(
        raw_text=execution.raw_response,
        action=(action if action is not None else execution.action).strip().upper(),
        public=(
            public_text if public_text is not None else execution.public_draft
        ).strip(),
        private=list(private or []),
        turn_targets=list(
            turn_target_ids if turn_target_ids is not None else execution.turn_targets or []
        ),
        scene_transition=(execution.scene_transition or None),
    )


def _apply_response_to_execution(
    execution: GameMasterExecution,
    response: GameMasterResponse,
) -> None:
    execution.state = GameMasterExecutionState.DRAFT
    execution.action = response.action
    execution.public_draft = response.public
    execution.private_drafts = response.private
    execution.turn_targets = response.turn_targets
    execution.scene_transition = response.scene_transition or {}
    execution.error = ""
    execution.raw_response = response.raw_text


def _run_provider_execution(
    *,
    execution: GameMasterExecution,
    config: GameMasterConfig,
    system_prompt: str,
    messages: list[dict],
) -> None:
    model_config = config.model_config
    if model_config is None or not model_config.enabled:
        GameMasterExecution.objects.filter(pk=execution.pk).update(
            state=GameMasterExecutionState.FAILED,
            error="Model GM has no enabled API model configured.",
        )
        return

    GameMasterExecution.objects.filter(pk=execution.pk).update(
        state=GameMasterExecutionState.RUNNING,
        model_used=model_config.gateway_model,
        error="",
        raw_response="",
        latency_ms=None,
    )
    started = time.perf_counter()
    raw = ""
    try:
        provider_response = get_llm_client().generate(
            system_prompt=system_prompt,
            messages=[
                *messages,
                {"role": "user", "content": gm_execution_request(execution.scene)},
            ],
            model=model_config.gateway_model,
            temperature=model_config.temperature,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        raw = provider_response.raw_text or ""
        response = parse_gm_response(raw, scene=execution.scene)
        response = _normalize_model_gm_response(
            response,
            scene=execution.scene,
        )
        execution.refresh_from_db()
        _apply_response_to_execution(execution, response)
        execution.latency_ms = elapsed
        execution.save(
            update_fields=[
                "state",
                "action",
                "public_draft",
                "private_drafts",
                "turn_targets",
                "scene_transition",
                "error",
                "raw_response",
                "latency_ms",
                "updated_at",
            ]
        )
    except ValidationError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        GameMasterExecution.objects.filter(pk=execution.pk).update(
            state=GameMasterExecutionState.INVALID,
            error=str(exc),
            raw_response=raw,
            latency_ms=elapsed,
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        GameMasterExecution.objects.filter(pk=execution.pk).update(
            state=GameMasterExecutionState.FAILED,
            error=str(exc),
            raw_response=getattr(exc, "raw_text", "") or raw,
            latency_ms=elapsed,
        )


def _prepare_external_execution(
    *,
    execution: GameMasterExecution,
    config: GameMasterConfig,
    system_prompt: str,
    messages: list[dict],
) -> None:
    prompt, is_bootstrap = _build_manual_chat_prompt(
        execution=execution,
        config=config,
        system_prompt=system_prompt,
        messages=messages,
    )
    label = (config.manual_chat_label or "Model GM").strip()
    GameMasterExecution.objects.filter(pk=execution.pk).update(
        state=GameMasterExecutionState.WAITING_EXTERNAL,
        error="",
        external_prompt=prompt,
        external_is_bootstrap=is_bootstrap,
        model_used=f"manual-chat:{label}",
        raw_response="",
        latency_ms=None,
    )


def _build_manual_chat_prompt(
    *,
    execution: GameMasterExecution,
    config: GameMasterConfig,
    system_prompt: str,
    messages: list[dict],
) -> tuple[str, bool]:
    mode = config.manual_chat_context_mode
    previous = _previous_external_execution(execution)
    use_delta = (
        mode == ManualChatContextMode.CHAT_MEMORY
        and config.manual_chat_initialized
        and previous is not None
        and previous.external_chat_url == execution.external_chat_url
        and previous.external_chat_label == execution.external_chat_label
    )

    if not use_delta:
        rendered = "\n\n".join(
            f"[{str(item.get('role', 'user')).upper()}]\n{item.get('content', '')}"
            for item in messages
        ) or "(no message history)"
        prompt = (
            "# MRAZ GAME MASTER CHAT BRIDGE\n"
            "You are taking the Game Master role for the application. The packet below "
            "is authoritative for this execution. Do not act as a player character.\n\n"
            "## SYSTEM PROMPT\n"
            + system_prompt
            + "\n\n## CHAT CONTEXT\n"
            + rendered
            + "\n\n"
            + gm_execution_request(execution.scene)
            + "\n\n## RESPONSE CONTRACT\n"
            + gm_response_contract()
        )
        return prompt, mode == ManualChatContextMode.CHAT_MEMORY

    previous_ids = set(previous.external_synced_message_ids or [])
    new_ids = [
        message_id
        for message_id in execution.context_message_ids
        if message_id not in previous_ids
    ]
    messages_by_id = {
        message.pk: message
        for message in Message.objects.filter(pk__in=new_ids)
        .select_related("author_player", "private_player")
    }
    updates = "\n\n".join(
        _manual_history_message(messages_by_id[message_id])
        for message_id in new_ids
        if message_id in messages_by_id
    ) or "(no new canon messages since the previous synchronized GM response)"

    scene = execution.scene
    control = [
        f"Scene: {scene.name}",
        f"Turn mode: {scene.mode}",
        "The CURRENT SCENE CONTROL below supersedes stale pre-transition location/state wording "
        "that may still appear in older conversation history.",
    ]
    if scene.description.strip():
        control.append("Scene description:\n" + scene.description.strip())
    if scene.memory_summary.strip():
        control.append("Scene memory:\n" + scene.memory_summary.strip())
    if scene.mode == TurnMode.ROUND:
        control.append(
            f"Round order IDs: {list(scene.round_order or [])}; "
            f"active index: {scene.active_player_index}"
        )
    elif scene.mode == TurnMode.SOFT_ROUND:
        control.append(
            "SOFT_ROUND: target only participant line(s) that have a meaningful beat now; "
            "do not alternate mechanically."
        )

    prompt = (
        "# MRAZ GAME MASTER CHAT BRIDGE · DELTA\n"
        "Continue as the SAME Game Master in this persistent external conversation. "
        "Only application canon is authoritative. Any earlier draft that the application "
        "did not publish must be ignored.\n\n"
        "## CURRENT SCENE CONTROL\n"
        + "\n\n".join(control)
        + "\n\n## NEW CANON SINCE LAST SYNC\n"
        + updates
        + "\n\n"
        + gm_execution_request(execution.scene)
        + "\n\n## RESPONSE CONTRACT\n"
        + gm_response_contract()
    )
    return prompt, False


def _previous_external_execution(
    execution: GameMasterExecution,
) -> GameMasterExecution | None:
    lineage_ids = [
        lineage_scene.pk
        for lineage_scene in get_scene_lineage(execution.scene)
    ]
    return (
        GameMasterExecution.objects.filter(
            scene_id__in=lineage_ids,
            transport=GameMasterTransport.MANUAL_CHAT,
            state=GameMasterExecutionState.PUBLISHED,
            pk__lt=execution.pk,
        )
        .order_by("-pk")
        .first()
    )


def _manual_history_message(message: Message) -> str:
    if message.visibility == Visibility.PUBLIC:
        scope = "PUBLIC"
    elif message.visibility == Visibility.PRIVATE_GM_PLAYER:
        target = (
            message.private_player.display_name
            if message.private_player_id and message.private_player
            else "unknown"
        )
        scope = f"PRIVATE GM↔{target}"
    else:
        scope = "GM_ONLY"

    if message.author_type == AuthorType.GM:
        author = "GM"
    elif message.author_player_id and message.author_player:
        author = message.author_player.display_name
    else:
        author = message.author_type
    action = f" [{message.action_type}]" if message.action_type else ""
    if message.author_type == AuthorType.PLAYER:
        return (
            f"[{scope}] {author}{action} [PLAYER DECLARATION; external-world claims "
            f"are not objective GM confirmation]:\n{message.content}"
        )
    return f"[{scope}] {author}{action}:\n{message.content}"


def _players_from_ids(scene: Scene, player_ids: list[int]) -> list[Player]:
    if not player_ids:
        return []
    players = {
        player.pk: player
        for player in Player.objects.filter(
            scene_participations__scene=scene,
            pk__in=player_ids,
        )
    }
    if len(players) != len(player_ids):
        raise ValidationError("GM turn target is not a participant in this scene.")
    return [players[player_id] for player_id in player_ids]


def _scene_players(scene: Scene) -> list[Player]:
    return list(
        Player.objects.filter(scene_participations__scene=scene)
        .order_by("scene_participations__order", "scene_participations__pk")
    )


def _decode_json_object(text: str) -> dict | None:
    stripped = text.strip()
    fence = chr(96) * 3
    if stripped.startswith(fence):
        lines = stripped.splitlines()
        if lines and lines[0].startswith(fence):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith(fence):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    candidates = [stripped]
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match and match.group(0) != stripped:
        candidates.append(match.group(0))

    for candidate in candidates:
        for strict in (True, False):
            try:
                obj = json.loads(candidate, strict=strict)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if isinstance(obj, dict):
                return obj
            if isinstance(obj, str):
                try:
                    nested = json.loads(obj, strict=False)
                except (json.JSONDecodeError, TypeError, ValueError):
                    nested = None
                if isinstance(nested, dict):
                    return nested
    return None
