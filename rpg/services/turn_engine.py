"""Turn orchestration for MRAZ Master.

Only explicit GM actions enter this service. Persisting an AI Message never
starts another model call.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max

from rpg.models import (
    AuthorType,
    ExecutionState,
    Message,
    ManualChatContextMode,
    MessageRevision,
    ModelConfig,
    Player,
    PlayerStatus,
    PlayerTransport,
    Scene,
    Turn,
    TurnExecution,
    TurnMode,
    TurnState,
    Visibility,
)
from rpg.services.context_builder import (
    build_player_context,
    get_player_history_messages,
    get_scene_lineage,
)
from rpg.services.llm import LLMResponse, get_llm_client, parse_structured_response

logger = logging.getLogger("rpg.turn_engine")


@dataclass
class TurnResult:
    turn: Turn
    messages: list[Message]


def start_turn(
    *,
    scene: Scene,
    gm_message_text: str | None = None,
    selected_players: list[Player] | None = None,
    private_to_player: Player | None = None,
    client_turn_id: str | uuid.UUID | None = None,
    silent: bool = False,
) -> TurnResult:
    """Create and execute one immutable GM-triggered turn.

    Private turns are independent from the public scene mode and never advance
    ROUND state. A repeated client_turn_id is idempotent and returns the
    existing turn without re-running model calls.
    """
    gm_message_text = (gm_message_text or "").strip()
    if silent:
        if private_to_player is not None:
            raise ValidationError("Silent turns are public only.")
        if gm_message_text:
            raise ValidationError("Silent turn cannot include a GM message.")
    elif not gm_message_text:
        raise ValidationError("GM message cannot be empty.")

    normalized_id = _normalize_client_turn_id(client_turn_id)
    selected_players = selected_players or []

    with transaction.atomic():
        locked_scene = (
            Scene.objects.select_for_update()
            .select_related("campaign")
            .get(pk=scene.pk)
        )

        if locked_scene.is_closed:
            raise ValidationError("This scene is closed and read-only.")

        if normalized_id is not None:
            existing = (
                Turn.objects.filter(scene=locked_scene, client_turn_id=normalized_id)
                .first()
            )
            if existing is not None:
                return TurnResult(
                    existing,
                    list(existing.messages.order_by("created_at")),
                )

        if TurnExecution.objects.filter(
            turn__scene=locked_scene,
            state=ExecutionState.WAITING_EXTERNAL,
        ).exists():
            raise ValidationError(
                "This scene already has a manual-chat execution waiting for a pasted "
                "response. Complete it before starting another model turn."
            )

        if private_to_player is not None:
            _ensure_scene_participant(locked_scene, private_to_player)
            mode = TurnMode.MANUAL
            targets = [private_to_player]
            active_player_id = None
            is_private = True
        else:
            mode = locked_scene.mode
            is_private = False
            if mode == TurnMode.ROUND:
                targets = _validated_round_players(locked_scene)
                active_player_id = (
                    targets[locked_scene.active_player_index].pk if targets else None
                )
            elif mode == TurnMode.MANUAL:
                targets = _validate_selected_players(locked_scene, selected_players)
                active_player_id = None
            elif mode == TurnMode.SIMULTANEOUS:
                targets = (
                    _validate_selected_players(locked_scene, selected_players)
                    if selected_players
                    else _scene_players(locked_scene)
                )
                active_player_id = None
            elif mode == TurnMode.TABLE:
                targets = (
                    _validate_selected_players(locked_scene, selected_players)
                    if selected_players
                    else _validated_round_players(locked_scene)
                )
                active_player_id = None
            else:
                raise ValidationError(f"Unsupported turn mode: {mode}")

        turn = Turn.objects.create(
            scene=locked_scene,
            mode=mode,
            state=TurnState.RUNNING,
            participants=[player.pk for player in targets],
            client_turn_id=normalized_id,
            is_private=is_private,
            active_player_id_snapshot=active_player_id,
        )

        if not silent:
            visibility = (
                Visibility.PRIVATE_GM_PLAYER if is_private else Visibility.PUBLIC
            )
            gm_message = Message.objects.create(
                campaign=locked_scene.campaign,
                scene=locked_scene,
                turn=turn,
                author_type=AuthorType.GM,
                content=gm_message_text,
                visibility=visibility,
                private_player=private_to_player if is_private else None,
            )
            turn.trigger_message = gm_message
            turn.save(update_fields=["trigger_message"])

        executions = []
        for index, player in enumerate(targets):
            nudge = (player.pending_nudge or "").strip()
            execution = TurnExecution.objects.create(
                turn=turn,
                player=player,
                order_index=index,
                nudge_text=nudge,
                transport=player.transport,
                external_context_mode=(
                    player.manual_chat_context_mode
                    if player.transport == PlayerTransport.MANUAL_CHAT
                    else ""
                ),
                external_chat_label=(
                    player.manual_chat_label
                    if player.transport == PlayerTransport.MANUAL_CHAT
                    else ""
                ),
                external_chat_url=(
                    player.manual_chat_url
                    if player.transport == PlayerTransport.MANUAL_CHAT
                    else ""
                ),
            )
            executions.append(execution)
            if nudge:
                Player.objects.filter(pk=player.pk).update(pending_nudge="")
                player.pending_nudge = ""

        # MANUAL/SIMULTANEOUS/ROUND/private freeze a per-player snapshot.
        # Each player may inherit a different predecessor chain, so one shared
        # message-id list would either lose history or leak another branch.
        if mode in (TurnMode.MANUAL, TurnMode.SIMULTANEOUS, TurnMode.ROUND) or is_private:
            for execution in executions:
                snapshot_ids = [
                    message.pk
                    for message in get_player_history_messages(
                        player=execution.player,
                        scene=locked_scene,
                    )
                ]
                TurnExecution.objects.filter(pk=execution.pk).update(
                    history_message_ids=snapshot_ids
                )
                execution.history_message_ids = snapshot_ids

    generated: list[Message] = []

    if mode == TurnMode.TABLE and not is_private:
        for execution in executions:
            history_ids = [
                message.pk
                for message in get_player_history_messages(
                    player=execution.player,
                    scene=scene,
                )
            ]
            TurnExecution.objects.filter(pk=execution.pk).update(
                history_message_ids=history_ids
            )
            execution.history_message_ids = history_ids

    manual_executions = [
        execution
        for execution in executions
        if execution.transport == PlayerTransport.MANUAL_CHAT
    ]
    provider_executions = [
        execution
        for execution in executions
        if execution.transport != PlayerTransport.MANUAL_CHAT
    ]

    for execution in manual_executions:
        out_of_turn = (
            mode == TurnMode.ROUND
            and execution.player_id != turn.active_player_id_snapshot
        )
        try:
            _prepare_external_execution(
                execution=execution,
                out_of_turn=out_of_turn,
            )
        except Exception as exc:
            _mark_execution_error(execution, exc)

    if provider_executions:
        client = get_llm_client()
        # ROUND contexts are frozen before any model call, so provider calls can run
        # concurrently without leaking another player's response into the same round.
        if mode == TurnMode.ROUND and not is_private and len(provider_executions) > 1:
            generated.extend(
                _run_round_parallel(
                    executions=provider_executions,
                    turn=turn,
                    client=client,
                )
            )
        else:
            for execution in provider_executions:
                out_of_turn = (
                    mode == TurnMode.ROUND
                    and execution.player_id != turn.active_player_id_snapshot
                )
                message = _run_execution(
                    execution=execution,
                    client=client,
                    out_of_turn=out_of_turn,
                )
                if message is not None:
                    generated.append(message)

    _refresh_turn_state(turn)
    turn.refresh_from_db()

    if mode == TurnMode.ROUND and not is_private and turn.state == TurnState.COMPLETED:
        _advance_round_once(turn)
        turn.refresh_from_db()

    return TurnResult(turn, generated)


def start_silent_turn(
    *,
    scene: Scene,
    selected_players: list[Player] | None = None,
    client_turn_id: str | uuid.UUID | None = None,
) -> TurnResult:
    """Run a public turn without creating a GM message.

    ROUND calls the full round roster and advances normally when all executions
    complete. MANUAL calls only the explicitly selected player(s).
    """
    if scene.mode not in (TurnMode.ROUND, TurnMode.MANUAL):
        raise ValidationError("Silence is available only in ROUND or MANUAL mode.")
    return start_turn(
        scene=scene,
        gm_message_text="",
        selected_players=selected_players,
        client_turn_id=client_turn_id,
        silent=True,
    )


def retry_execution(
    execution: TurnExecution,
    *,
    model_config: ModelConfig | None = None,
) -> TurnResult:
    """Retry one FAILED/INVALID execution using its original frozen context."""
    with transaction.atomic():
        execution = (
            TurnExecution.objects.select_for_update()
            .select_related("turn__scene__campaign", "player")
            .get(pk=execution.pk)
        )
        if execution.turn.scene.is_closed:
            raise RuntimeError("Cannot retry an execution in a closed scene")
        if execution.state not in (ExecutionState.FAILED, ExecutionState.INVALID):
            raise RuntimeError("Can only retry a FAILED or INVALID execution")
        execution.state = ExecutionState.PENDING
        execution.error = ""
        execution.action_type = ""
        execution.save(update_fields=["state", "error", "action_type", "updated_at"])

    turn = execution.turn
    out_of_turn = (
        turn.mode == TurnMode.ROUND
        and execution.player_id != turn.active_player_id_snapshot
    )
    if execution.transport == PlayerTransport.MANUAL_CHAT:
        _prepare_external_execution(
            execution=execution,
            out_of_turn=out_of_turn,
        )
        message = None
    else:
        message = _run_execution(
            execution=execution,
            client=get_llm_client(),
            out_of_turn=out_of_turn,
            model_config_override=model_config,
        )
    _refresh_turn_state(turn)
    turn.refresh_from_db()
    if (
        turn.mode == TurnMode.ROUND
        and not turn.is_private
        and turn.state == TurnState.COMPLETED
    ):
        _advance_round_once(turn)
        turn.refresh_from_db()
    return TurnResult(turn, [message] if message is not None else [])


def regenerate_execution(
    execution: TurnExecution,
    *,
    model_config: ModelConfig | None = None,
) -> TurnResult:
    """Regenerate one successful public execution and replace its visible reply in place.

    The original frozen history and GM trigger are reused. Other players are never
    called, and ROUND advancement is never repeated.
    """
    execution = (
        TurnExecution.objects.select_related("turn__scene__campaign", "player")
        .get(pk=execution.pk)
    )
    turn = execution.turn
    scene = turn.scene
    player = execution.player

    if scene.is_closed:
        raise RuntimeError("Cannot regenerate an execution in a closed scene")
    if turn.is_private:
        raise RuntimeError("Only public executions can be regenerated here")
    if execution.state != ExecutionState.COMPLETED:
        raise RuntimeError("Only a COMPLETED execution can be regenerated")
    if execution.transport == PlayerTransport.MANUAL_CHAT:
        raise RuntimeError(
            "Manual-chat executions are regenerated in the external chat, not through LiteLLM."
        )

    public_message = (
        Message.objects.filter(
            execution=execution,
            author_type=AuthorType.PLAYER,
            visibility=Visibility.PUBLIC,
        )
        .order_by("pk")
        .first()
    )
    if public_message is None:
        raise RuntimeError("Execution has no public player message to regenerate")

    out_of_turn = (
        turn.mode == TurnMode.ROUND
        and execution.player_id != turn.active_player_id_snapshot
    )

    _set_player_status(player, PlayerStatus.GENERATING)
    try:
        response = _generate_execution_response(
            execution=execution,
            client=get_llm_client(),
            out_of_turn=out_of_turn,
            model_config_override=model_config,
        )
        _validate_action(turn=turn, out_of_turn=out_of_turn, response=response)
        _validate_response_discipline(
            turn=turn,
            out_of_turn=out_of_turn,
            response=response,
        )
        _require_public_body(response)

        action = response.action_type or "ACT"
        public_text = (response.public or "").strip()
        private_text = (response.private_to_gm or "").strip()

        with transaction.atomic():
            locked_execution = (
                TurnExecution.objects.select_for_update()
                .select_related("turn__scene", "player")
                .get(pk=execution.pk)
            )
            if locked_execution.state != ExecutionState.COMPLETED:
                raise RuntimeError("Execution changed state during regeneration")

            locked_message = Message.objects.select_for_update().get(pk=public_message.pk)
            if not locked_message.revisions.exists():
                _record_message_revision(locked_message, reason="ORIGINAL")
            locked_message.content = public_text or f"[{action}] {player.display_name}"
            locked_message.action_type = action
            locked_message.save(update_fields=["content", "action_type"])
            _record_message_revision(locked_message, reason="REGEN")

            Message.objects.filter(
                execution=locked_execution,
                author_type=AuthorType.PLAYER,
                visibility=Visibility.PRIVATE_GM_PLAYER,
            ).delete()

            if private_text and private_text != public_text:
                Message.objects.create(
                    campaign=scene.campaign,
                    scene=scene,
                    turn=turn,
                    execution=locked_execution,
                    author_type=AuthorType.PLAYER,
                    author_player=player,
                    content=private_text,
                    visibility=Visibility.PRIVATE_GM_PLAYER,
                    private_player=player,
                    action_type=action,
                    gm_unread=True,
                )

            locked_execution.action_type = action
            locked_execution.error = ""
            locked_execution.save(
                update_fields=["action_type", "error", "updated_at"]
            )

        public_message.refresh_from_db()
        return TurnResult(turn, [public_message])
    except Exception as exc:
        logger.warning(
            "Regeneration failed for player %s: %s",
            player.display_name,
            exc,
        )
        raise RuntimeError(f"Regeneration failed: {exc}") from exc
    finally:
        _set_player_status(player, PlayerStatus.IDLE)


def revise_execution_ooc(
    *,
    public_message: Message,
    gm_comment: str,
) -> tuple[Message, bool]:
    """Send private OOC feedback and let the same model revise its public declaration.

    The public Message row is updated in place when the model chooses a new
    declaration, so links and round grouping stay stable.
    """
    comment = (gm_comment or "").strip()
    if not comment:
        raise ValidationError("OOC comment cannot be empty.")

    public_message = (
        Message.objects.select_related(
            "scene__campaign",
            "author_player__model_config",
            "execution__turn",
        )
        .get(pk=public_message.pk)
    )
    scene = public_message.scene
    player = public_message.author_player
    execution = public_message.execution

    if scene is None or scene.is_closed:
        raise RuntimeError("Cannot send OOC feedback in a closed or missing scene")
    if (
        public_message.author_type != AuthorType.PLAYER
        or public_message.visibility != Visibility.PUBLIC
        or player is None
        or execution is None
    ):
        raise RuntimeError("OOC feedback requires a public player execution message")
    if execution.state != ExecutionState.COMPLETED:
        raise RuntimeError("OOC feedback requires a completed player execution")
    if execution.transport == PlayerTransport.MANUAL_CHAT:
        raise RuntimeError(
            "Manual-chat declarations must be revised in the external chat."
        )

    turn = execution.turn
    if turn.is_private:
        raise RuntimeError("OOC revision applies only to public declarations")

    ooc_message = Message.objects.create(
        campaign=scene.campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content=(
            f"[OOC к публичной заявке #{public_message.pk}]\n"
            f"{comment}"
        ),
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=player,
    )

    out_of_turn = (
        turn.mode == TurnMode.ROUND
        and execution.player_id != turn.active_player_id_snapshot
    )
    extra_prompt = (
        "\n\n# OOC REVISION\n"
        "The GM has sent private out-of-character feedback about your existing "
        "public declaration. This is meta-level guidance, not in-fiction dialogue "
        "and not a new turn. Reconsider ONLY that declaration.\n"
        "Return the usual JSON object. In action_type and public, provide the FULL "
        "replacement declaration that should now stand in the public scene. You may "
        "change the declaration if the GM feedback warrants it. If you prefer to keep "
        "your original declaration, repeat its action_type and public text exactly. "
        "Do not add a second action or advance the scene beyond this declaration. "
        "The replacement must obey the same ROUND role and response limits as the "
        "original. private_to_gm may contain a brief OOC note to the GM."
    )

    _set_player_status(player, PlayerStatus.GENERATING)
    try:
        response = _generate_execution_response(
            execution=execution,
            client=get_llm_client(),
            out_of_turn=out_of_turn,
            trigger_message=ooc_message,
            extra_history=[public_message, ooc_message],
            extra_system_prompt=extra_prompt,
        )
        _validate_action(turn=turn, out_of_turn=out_of_turn, response=response)
        _validate_response_discipline(
            turn=turn,
            out_of_turn=out_of_turn,
            response=response,
        )
        _require_public_body(response)

        action = response.action_type or "ACT"
        public_text = (response.public or "").strip()
        old_text = public_message.content
        old_action = public_message.action_type or "ACT"
        changed = public_text != old_text or action != old_action

        with transaction.atomic():
            locked_execution = TurnExecution.objects.select_for_update().get(
                pk=execution.pk
            )
            if locked_execution.state != ExecutionState.COMPLETED:
                raise RuntimeError("Execution changed state during OOC revision")

            locked_message = Message.objects.select_for_update().get(
                pk=public_message.pk
            )
            if changed:
                if not locked_message.revisions.exists():
                    _record_message_revision(locked_message, reason="ORIGINAL")
                locked_message.content = (
                    public_text or f"[{action}] {player.display_name}"
                )
                locked_message.action_type = action
                locked_message.save(update_fields=["content", "action_type"])
                _record_message_revision(locked_message, reason="OOC")
                locked_execution.action_type = action
                locked_execution.save(
                    update_fields=["action_type", "updated_at"]
                )

            private_note = (response.private_to_gm or "").strip()
            status_text = (
                f"[OOC к заявке #{public_message.pk}] Заявка изменена."
                if changed
                else f"[OOC к заявке #{public_message.pk}] Оставляю заявку без изменений."
            )
            if private_note:
                status_text += "\n\n" + private_note

            player_reply = Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                author_type=AuthorType.PLAYER,
                author_player=player,
                content=status_text,
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
                action_type="",
                gm_unread=True,
            )

        return player_reply, changed
    except Exception as exc:
        logger.warning(
            "OOC revision failed for player %s: %s",
            player.display_name,
            exc,
        )
        if isinstance(exc, ValidationError):
            raise
        raise RuntimeError(f"OOC revision failed: {exc}") from exc
    finally:
        _set_player_status(player, PlayerStatus.IDLE)


def retry_turn(turn: Turn) -> TurnResult:
    """Compatibility helper: retry only failed executions, never successes."""
    failed = list(
        turn.executions.filter(
            state__in=[ExecutionState.FAILED, ExecutionState.INVALID]
        ).order_by("order_index", "pk")
    )
    if not failed:
        raise RuntimeError("Turn has no failed executions to retry")

    messages: list[Message] = []
    for execution in failed:
        result = retry_execution(execution)
        messages.extend(result.messages)
    turn.refresh_from_db()
    return TurnResult(turn, messages)


def _execution_history(
    execution: TurnExecution,
    *,
    extra_history: list[Message] | None = None,
) -> list[Message]:
    frozen_messages = {
        message.pk: message
        for message in Message.objects.filter(pk__in=execution.history_message_ids)
        .select_related("author_player")
    }
    history = [
        frozen_messages[message_id]
        for message_id in execution.history_message_ids
        if message_id in frozen_messages
    ]
    if extra_history:
        known_ids = {message.pk for message in history if message.pk}
        for message in extra_history:
            if message.pk and message.pk in known_ids:
                continue
            history.append(message)
            if message.pk:
                known_ids.add(message.pk)
    return history


def _append_round_role_prompt(context, *, turn: Turn, out_of_turn: bool) -> None:
    if out_of_turn:
        context.system_prompt += (
            "\n\n# ROUND ROLE\n"
            "You are NOT the active player this round. Your default and expected "
            "response is PASS. Use ACT_OUT_OF_TURN only for a genuinely urgent, "
            "immediate intervention that cannot reasonably wait for your own turn. "
            "Ordinary conversation, commentary, exposition, volunteering information, "
            "and non-urgent questions must wait. You may only PASS or ACT_OUT_OF_TURN. "
            "If you use ACT_OUT_OF_TURN, the HARD LIMIT is 650 visible characters, "
            "2 paragraphs, and 1 direct question."
        )
    elif turn.mode == TurnMode.ROUND and not turn.is_private:
        context.system_prompt += (
            "\n\n# ROUND ROLE\n"
            "You are the active player this round. You may only ACT or PASS. "
            "For ACT, the HARD LIMIT is 1200 visible characters, 6 paragraphs, "
            "and 2 direct questions. Six paragraphs is a ceiling, not a target; "
            "prefer 2-3 paragraphs unless more are genuinely necessary."
        )


def _build_execution_request(
    *,
    execution: TurnExecution,
    out_of_turn: bool,
    trigger_message: Message | None = None,
    extra_history: list[Message] | None = None,
    extra_system_prompt: str = "",
    model_config_override: ModelConfig | None = None,
) -> tuple[str, list[dict], str, float]:
    execution.refresh_from_db()
    turn = execution.turn
    scene = turn.scene
    player = execution.player

    context = build_player_context(
        player=player,
        scene=scene,
        trigger_message=trigger_message or turn.trigger_message,
        history=_execution_history(execution, extra_history=extra_history),
    )
    _append_round_role_prompt(context, turn=turn, out_of_turn=out_of_turn)
    if turn.trigger_message_id is None:
        context.system_prompt += (
            "\n\n# GM SILENCE\n"
            "The GM has deliberately chosen SILENCE for this turn. There is no new GM "
            "statement, event, or hidden instruction to infer. Continue only from the "
            "already established scene state and visible history. Treat this as the GM "
            "yielding the floor to the players. Do not invent a GM action just to create "
            "a prompt. In ROUND, normal active/inactive role rules still apply."
        )
    if execution.nudge_text.strip():
        context.system_prompt += (
            "\n\n# ONE-SHOT GM NUDGE\n"
            "This is private meta-level guidance from the GM for this execution only. "
            "Do not quote it, mention it, or treat it as in-fiction dialogue:\n"
            + execution.nudge_text.strip()
        )
    if extra_system_prompt:
        context.system_prompt += extra_system_prompt

    model = (
        model_config_override.gateway_model
        if model_config_override and model_config_override.enabled
        else _player_model(player)
    )
    temperature = (
        model_config_override.temperature
        if model_config_override and model_config_override.enabled
        else _player_temp(player)
    )
    TurnExecution.objects.filter(pk=execution.pk).update(
        model_used=model,
        system_prompt_snapshot=context.system_prompt,
        request_messages=context.messages,
        raw_response="",
        latency_ms=None,
    )
    return context.system_prompt, context.messages, model, temperature


def _manual_response_contract() -> str:
    return (
        "Return ONLY one response for this RPG execution. Prefer the exact JSON envelope:\n"
        '{"action_type":"ACT|PASS|ACT_OUT_OF_TURN","public":"...","private_to_gm":"..."}\n'
        "Do not wrap the JSON in commentary. The application will parse and validate the "
        "result. Preserve [[SPEECH]]original[[RU]]Russian translation[[/SPEECH]] markup "
        "for spoken dialogue. private_to_gm should stay empty unless there is a materially "
        "important secret for the GM."
    )


def _manual_history_message(message: Message) -> str:
    if message.author_type == AuthorType.GM:
        author = "GM"
    elif message.author_player_id:
        author = message.author_player.display_name
    else:
        author = message.author_type

    action = f" [{message.action_type}]" if message.action_type else ""
    scope = (
        "PRIVATE GM↔YOU"
        if message.visibility == Visibility.PRIVATE_GM_PLAYER
        else "PUBLIC"
    )
    return f"[{scope}] {author}{action}:\n{message.content}"


def _previous_external_execution(execution: TurnExecution) -> TurnExecution | None:
    return (
        TurnExecution.objects.filter(
            player_id=execution.player_id,
            transport=PlayerTransport.MANUAL_CHAT,
            state=ExecutionState.COMPLETED,
            pk__lt=execution.pk,
        )
        .exclude(external_synced_message_ids=[])
        .order_by("-pk")
        .first()
    )


def _manual_delta_constraints(execution: TurnExecution, *, out_of_turn: bool) -> str:
    turn = execution.turn
    scene = turn.scene
    lines = [
        f"Scene: {scene.name}",
        f"Turn mode: {turn.mode}",
    ]
    if scene.dialogue_language.strip():
        lines.append(f"Default spoken language: {scene.dialogue_language.strip()}")
    if scene.description.strip():
        lines.append("Current scene description:\n" + scene.description.strip())
    if scene.campaign.shared_memory.strip():
        lines.append(
            "Current shared campaign memory:\n"
            + scene.campaign.shared_memory.strip()
        )
    if execution.player.memory_summary.strip():
        lines.append(
            "Current private long-term memory:\n"
            + execution.player.memory_summary.strip()
        )
    if scene.memory_summary.strip():
        lines.append("Current scene memory:\n" + scene.memory_summary.strip())

    if turn.mode == TurnMode.ROUND and not turn.is_private:
        if out_of_turn:
            lines.append(
                "ROUND role: INACTIVE. PASS is expected. ACT_OUT_OF_TURN is allowed only "
                "for one genuinely urgent intervention that cannot wait. Hard limit: "
                "650 visible characters, 2 paragraphs, 1 direct question."
            )
        else:
            lines.append(
                "ROUND role: ACTIVE. You may ACT or PASS. ACT hard limit: 1200 visible "
                "characters, 6 paragraphs, 2 direct questions; normally prefer 2-3 paragraphs."
            )
    if turn.trigger_message_id is None:
        lines.append(
            "GM SILENCE: there is no new GM event or hidden instruction. Continue only "
            "from established state and visible history."
        )
    if execution.nudge_text.strip():
        lines.append(
            "ONE-SHOT GM NUDGE (meta, not fiction): " + execution.nudge_text.strip()
        )
    return "\n".join(lines)


def _build_manual_chat_prompt(
    *,
    execution: TurnExecution,
    out_of_turn: bool,
    system_prompt: str,
    messages: list[dict],
) -> tuple[str, bool]:
    player = execution.player
    mode = execution.external_context_mode or ManualChatContextMode.FULL
    previous = _previous_external_execution(execution)
    current_lineage_ids = {
        lineage_scene.pk for lineage_scene in get_scene_lineage(execution.turn.scene)
    }
    previous_is_in_lineage = (
        previous is not None
        and previous.turn.scene_id in current_lineage_ids
    )
    previous_is_same_chat = (
        previous is not None
        and previous.external_chat_url == execution.external_chat_url
        and previous.external_chat_label == execution.external_chat_label
    )
    use_delta = (
        mode == ManualChatContextMode.CHAT_MEMORY
        and player.manual_chat_initialized
        and previous is not None
        and previous_is_in_lineage
        and previous_is_same_chat
        and bool(previous.external_synced_message_ids)
    )

    if not use_delta:
        rendered_messages = []
        for item in messages:
            role = str(item.get("role", "user")).upper()
            rendered_messages.append(f"[{role}]\n{item.get('content', '')}")
        prompt = (
            "# MRAZ MANUAL CHAT BRIDGE\n"
            "This message comes from the GM application. You are the LLM player for "
            f"{player.display_name}. Treat the SYSTEM PROMPT below as authoritative. "
            "The external conversation may keep its own memory, but this full packet is "
            "the authoritative application state for this execution.\n\n"
            "## SYSTEM PROMPT\n"
            + system_prompt
            + "\n\n## CHAT CONTEXT\n"
            + ("\n\n".join(rendered_messages) if rendered_messages else "(no chat history)")
            + "\n\n## RESPONSE CONTRACT\n"
            + _manual_response_contract()
        )
        return prompt, mode == ManualChatContextMode.CHAT_MEMORY

    previously_synced = set(previous.external_synced_message_ids or [])
    new_ids = [
        message_id
        for message_id in execution.history_message_ids
        if message_id not in previously_synced
    ]
    if execution.turn.trigger_message_id and execution.turn.trigger_message_id not in new_ids:
        if execution.turn.trigger_message_id not in previously_synced:
            new_ids.append(execution.turn.trigger_message_id)

    by_id = {
        message.pk: message
        for message in Message.objects.filter(pk__in=new_ids)
        .select_related("author_player")
    }
    new_messages = [
        by_id[message_id]
        for message_id in new_ids
        if message_id in by_id
    ]
    updates = (
        "\n\n".join(_manual_history_message(message) for message in new_messages)
        or "(no new visible messages since the last synchronized external response)"
    )
    accepted_messages = list(
        Message.objects.filter(
            execution=previous,
            author_type=AuthorType.PLAYER,
        )
        .select_related("author_player")
        .order_by("created_at", "pk")
    )
    accepted = (
        "\n\n".join(
            _manual_history_message(message) for message in accepted_messages
        )
        or "(no accepted player message was stored)"
    )
    prompt = (
        "# MRAZ MANUAL CHAT BRIDGE · DELTA\n"
        "Continue the SAME RPG character in this SAME persistent external conversation. "
        "Keep the full character/world/rules context already established earlier in this "
        "chat. The application is intentionally sending only changes since the last "
        "successfully imported response. Do not invent missing changes. Drafts or rejected "
        "answers that may exist in this web chat are NOT canon unless the application "
        "accepted them.\n\n"
        "## LAST RESPONSE ACCEPTED BY THE APPLICATION\n"
        + accepted
        + "\n\n## CURRENT EXECUTION CONSTRAINTS\n"
        + _manual_delta_constraints(execution, out_of_turn=out_of_turn)
        + "\n\n## NEW CONTEXT SINCE LAST SYNC\n"
        + updates
        + "\n\n## RESPONSE CONTRACT\n"
        + _manual_response_contract()
    )
    return prompt, False


def _prepare_external_execution(
    *,
    execution: TurnExecution,
    out_of_turn: bool,
) -> None:
    execution.refresh_from_db()
    system_prompt, messages, _, _ = _build_execution_request(
        execution=execution,
        out_of_turn=out_of_turn,
    )
    prompt, is_bootstrap = _build_manual_chat_prompt(
        execution=execution,
        out_of_turn=out_of_turn,
        system_prompt=system_prompt,
        messages=messages,
    )
    label = (execution.external_chat_label or execution.player.display_name).strip()
    TurnExecution.objects.filter(pk=execution.pk).update(
        state=ExecutionState.WAITING_EXTERNAL,
        error="",
        external_prompt=prompt,
        external_is_bootstrap=is_bootstrap,
        model_used=f"manual-chat:{label}",
        raw_response="",
        latency_ms=None,
    )
    execution.state = ExecutionState.WAITING_EXTERNAL
    execution.external_prompt = prompt
    execution.external_is_bootstrap = is_bootstrap
    _set_player_status(execution.player, PlayerStatus.WAITING_EXTERNAL)


def submit_external_response(
    *,
    execution: TurnExecution,
    raw_text: str,
) -> TurnResult:
    raw = (raw_text or "").strip()

    execution = (
        TurnExecution.objects.select_related("turn__scene__campaign", "player")
        .get(pk=execution.pk)
    )
    turn = execution.turn
    scene = turn.scene
    player = execution.player

    if scene.is_closed:
        raise ValidationError("Cannot import an external response into a closed scene.")
    if execution.transport != PlayerTransport.MANUAL_CHAT:
        raise ValidationError("Execution is not a manual-chat execution.")
    if execution.state != ExecutionState.WAITING_EXTERNAL:
        raise ValidationError("Execution is not waiting for an external response.")
    if not raw:
        TurnExecution.objects.filter(pk=execution.pk).update(
            error="External response cannot be empty.",
            raw_response="",
        )
        _set_player_status(player, PlayerStatus.WAITING_EXTERNAL)
        raise ValidationError("External response cannot be empty.")

    out_of_turn = (
        turn.mode == TurnMode.ROUND
        and execution.player_id != turn.active_player_id_snapshot
    )

    try:
        response = parse_structured_response(raw)
        _validate_action(turn=turn, out_of_turn=out_of_turn, response=response)
        _validate_response_discipline(
            turn=turn,
            out_of_turn=out_of_turn,
            response=response,
        )
        _require_public_body(response)

        message = _persist_player_response(
            execution=execution,
            response=response,
        )
        created_ids = list(
            Message.objects.filter(execution=execution)
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)
        )
        synced_ids = list(dict.fromkeys([
            *execution.history_message_ids,
            *created_ids,
        ]))
        TurnExecution.objects.filter(pk=execution.pk).update(
            state=ExecutionState.COMPLETED,
            action_type=response.action_type,
            error="",
            raw_response=raw,
            external_synced_message_ids=synced_ids,
        )
        if (
            execution.external_context_mode == ManualChatContextMode.CHAT_MEMORY
            and execution.external_is_bootstrap
        ):
            Player.objects.filter(pk=player.pk).update(manual_chat_initialized=True)
            player.manual_chat_initialized = True
        _set_player_status(player, PlayerStatus.IDLE)
    except Exception as exc:
        TurnExecution.objects.filter(pk=execution.pk).update(
            state=ExecutionState.WAITING_EXTERNAL,
            error=str(exc),
            raw_response=raw,
        )
        _set_player_status(player, PlayerStatus.WAITING_EXTERNAL)
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"External response rejected: {exc}") from exc

    _refresh_turn_state(turn)
    turn.refresh_from_db()
    if (
        turn.mode == TurnMode.ROUND
        and not turn.is_private
        and turn.state == TurnState.COMPLETED
    ):
        _advance_round_once(turn)
        turn.refresh_from_db()
    return TurnResult(turn, [message])


def _provider_generate(client, *, system_prompt, messages, model, temperature):
    started = time.perf_counter()
    try:
        response = client.generate(
            system_prompt=system_prompt,
            messages=messages,
            model=model,
            temperature=temperature,
        )
    except Exception as exc:
        return None, int((time.perf_counter() - started) * 1000), exc
    return response, int((time.perf_counter() - started) * 1000), None


def _generate_execution_response(
    *,
    execution: TurnExecution,
    client,
    out_of_turn: bool,
    trigger_message: Message | None = None,
    extra_history: list[Message] | None = None,
    extra_system_prompt: str = "",
    model_config_override: ModelConfig | None = None,
) -> LLMResponse:
    system_prompt, messages, model, temperature = _build_execution_request(
        execution=execution,
        out_of_turn=out_of_turn,
        trigger_message=trigger_message,
        extra_history=extra_history,
        extra_system_prompt=extra_system_prompt,
        model_config_override=model_config_override,
    )
    response, elapsed, error = _provider_generate(
        client,
        system_prompt=system_prompt,
        messages=messages,
        model=model,
        temperature=temperature,
    )
    if error is not None:
        TurnExecution.objects.filter(pk=execution.pk).update(
            latency_ms=elapsed,
            raw_response=getattr(error, "raw_text", "") or "",
        )
        raise error
    TurnExecution.objects.filter(pk=execution.pk).update(
        raw_response=response.raw_text or "",
        latency_ms=elapsed,
    )
    return response


def _require_public_body(response: LLMResponse) -> None:
    action = (response.action_type or "ACT").upper()
    if action != "PASS" and not (response.public or "").strip():
        raise InvalidActionError("Non-PASS response must contain a public declaration")


def _finalize_execution_response(
    *,
    execution: TurnExecution,
    response: LLMResponse,
    out_of_turn: bool,
) -> Message | None:
    turn = execution.turn
    player = execution.player
    _validate_action(turn=turn, out_of_turn=out_of_turn, response=response)
    _validate_response_discipline(
        turn=turn,
        out_of_turn=out_of_turn,
        response=response,
    )
    message = _persist_player_response(
        execution=execution,
        response=response,
    )
    TurnExecution.objects.filter(pk=execution.pk).update(
        state=ExecutionState.COMPLETED,
        action_type=response.action_type,
        error="",
    )
    _set_player_status(player, PlayerStatus.IDLE)
    return message


def _mark_execution_error(execution: TurnExecution, exc: Exception) -> None:
    player = execution.player
    if isinstance(exc, InvalidActionError):
        logger.info("Invalid action from %s: %s", player.display_name, exc)
        TurnExecution.objects.filter(pk=execution.pk).update(
            state=ExecutionState.INVALID,
            error=str(exc),
        )
    else:
        logger.warning("LLM call failed for player %s: %s", player.display_name, exc)
        TurnExecution.objects.filter(pk=execution.pk).update(
            state=ExecutionState.FAILED,
            error=str(exc),
        )
    _set_player_status(player, PlayerStatus.ERROR)


def _run_round_parallel(
    *,
    executions: list[TurnExecution],
    turn: Turn,
    client,
) -> list[Message]:
    prepared = []
    for execution in executions:
        execution.refresh_from_db()
        out_of_turn = execution.player_id != turn.active_player_id_snapshot
        TurnExecution.objects.filter(pk=execution.pk).update(
            state=ExecutionState.RUNNING,
            error="",
        )
        _set_player_status(execution.player, PlayerStatus.GENERATING)
        try:
            system_prompt, messages, model, temperature = _build_execution_request(
                execution=execution,
                out_of_turn=out_of_turn,
            )
        except Exception as exc:
            _mark_execution_error(execution, exc)
            continue
        prepared.append(
            (execution, out_of_turn, system_prompt, messages, model, temperature)
        )

    if not prepared:
        return []

    generated = []
    with ThreadPoolExecutor(max_workers=min(6, len(prepared))) as pool:
        futures = [
            (
                execution,
                out_of_turn,
                pool.submit(
                    _provider_generate,
                    client,
                    system_prompt=system_prompt,
                    messages=messages,
                    model=model,
                    temperature=temperature,
                ),
            )
            for execution, out_of_turn, system_prompt, messages, model, temperature in prepared
        ]
        for execution, out_of_turn, future in futures:
            response, elapsed, error = future.result()
            if error is not None:
                TurnExecution.objects.filter(pk=execution.pk).update(
                    latency_ms=elapsed,
                    raw_response=getattr(error, "raw_text", "") or "",
                )
                _mark_execution_error(execution, error)
                continue
            TurnExecution.objects.filter(pk=execution.pk).update(
                raw_response=response.raw_text or "",
                latency_ms=elapsed,
            )
            try:
                message = _finalize_execution_response(
                    execution=execution,
                    response=response,
                    out_of_turn=out_of_turn,
                )
            except Exception as exc:
                _mark_execution_error(execution, exc)
                continue
            if message is not None:
                generated.append(message)
    return generated


def _run_execution(
    *,
    execution: TurnExecution,
    client,
    out_of_turn: bool,
    model_config_override: ModelConfig | None = None,
) -> Message | None:
    execution.refresh_from_db()
    turn = execution.turn
    scene = turn.scene
    player = execution.player

    TurnExecution.objects.filter(pk=execution.pk).update(
        state=ExecutionState.RUNNING,
        error="",
    )
    _set_player_status(player, PlayerStatus.GENERATING)

    try:
        response = _generate_execution_response(
            execution=execution,
            client=client,
            out_of_turn=out_of_turn,
            model_config_override=model_config_override,
        )
        return _finalize_execution_response(
            execution=execution,
            response=response,
            out_of_turn=out_of_turn,
        )
    except Exception as exc:
        _mark_execution_error(execution, exc)
        return None


class InvalidActionError(ValueError):
    pass


_SPEECH_BUDGET_RE = re.compile(
    r"\[\[SPEECH\]\](.*?)\[\[RU\]\].*?\[\[/SPEECH\]\]",
    flags=re.DOTALL,
)
_CONTROL_MARKUP_RE = re.compile(r"\[\[[^\]]+\]\]")


def _response_budget_text(text: str) -> str:
    """Approximate what the user visibly reads, excluding hidden RU translations."""
    text = _SPEECH_BUDGET_RE.sub(lambda match: match.group(1), text or "")
    return _CONTROL_MARKUP_RE.sub("", text).strip()


def _validate_response_discipline(
    *,
    turn: Turn,
    out_of_turn: bool,
    response: LLMResponse,
) -> None:
    if turn.is_private:
        return

    action = (response.action_type or "ACT").upper()
    if action == "PASS":
        return

    visible = _response_budget_text(response.public or "")
    if not visible:
        return

    max_chars = 650 if out_of_turn else 1200
    max_paragraphs = 2 if out_of_turn else 6
    max_questions = 1 if out_of_turn else 2

    if len(visible) > max_chars:
        kind = "ACT_OUT_OF_TURN" if out_of_turn else "ACT"
        raise InvalidActionError(
            f"{kind} response is too long ({len(visible)} visible chars; max {max_chars}). "
            "Keep to one immediate beat."
        )

    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n+", visible)
        if paragraph.strip()
    ]
    if len(paragraphs) > max_paragraphs:
        kind = "ACT_OUT_OF_TURN" if out_of_turn else "ACT"
        raise InvalidActionError(
            f"{kind} response has {len(paragraphs)} paragraphs; "
            f"maximum is {max_paragraphs}"
        )

    question_count = visible.count("?") + visible.count("？")
    if question_count > max_questions:
        kind = "ACT_OUT_OF_TURN" if out_of_turn else "ACT"
        raise InvalidActionError(
            f"{kind} response asks {question_count} questions; "
            f"maximum is {max_questions}"
        )


def _validate_action(*, turn: Turn, out_of_turn: bool, response: LLMResponse) -> None:
    if turn.is_private or turn.mode != TurnMode.ROUND:
        return
    action = response.action_type or "ACT"
    allowed = (
        {"PASS", "ACT_OUT_OF_TURN"}
        if out_of_turn
        else {"ACT", "PASS"}
    )
    if action not in allowed:
        role = "inactive" if out_of_turn else "active"
        raise InvalidActionError(
            f"{role} ROUND player returned forbidden action {action}; "
            f"allowed: {sorted(allowed)}"
        )


def _persist_player_response(
    *,
    execution: TurnExecution,
    response: LLMResponse,
) -> Message:
    turn = execution.turn
    scene = turn.scene
    player = execution.player
    action = response.action_type or "ACT"

    with transaction.atomic():
        if turn.is_private:
            main_message = Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                turn=turn,
                execution=execution,
                author_type=AuthorType.PLAYER,
                author_player=player,
                content=response.public or f"[{action}] {player.display_name}",
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
                action_type=action,
                gm_unread=True,
            )
        else:
            main_message = Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                turn=turn,
                execution=execution,
                author_type=AuthorType.PLAYER,
                author_player=player,
                content=response.public or f"[{action}] {player.display_name}",
                visibility=Visibility.PUBLIC,
                action_type=action,
            )
            _record_message_revision(main_message, reason="ORIGINAL")

        private_text = (response.private_to_gm or "").strip()
        public_text = (response.public or "").strip()
        if private_text and private_text != public_text:
            Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                turn=turn,
                execution=execution,
                author_type=AuthorType.PLAYER,
                author_player=player,
                content=private_text,
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
                action_type=action,
                gm_unread=True,
            )

    return main_message


def _record_message_revision(message: Message, *, reason: str) -> MessageRevision:
    current_max = (
        MessageRevision.objects.filter(message=message)
        .aggregate(value=Max("revision_index"))
        .get("value")
        or 0
    )
    return MessageRevision.objects.create(
        message=message,
        revision_index=current_max + 1,
        content=message.content,
        action_type=message.action_type,
        reason=reason,
    )


def ensure_message_revision(message: Message) -> list[MessageRevision]:
    if not message.revisions.exists():
        _record_message_revision(message, reason="ORIGINAL")
    return list(message.revisions.order_by("revision_index", "pk"))


def restore_message_revision(
    *,
    message: Message,
    revision: MessageRevision,
) -> Message:
    message = Message.objects.select_related("scene", "execution").get(pk=message.pk)
    if message.scene is None or message.scene.is_closed:
        raise RuntimeError("Cannot restore a message in a closed scene")
    if revision.message_id != message.pk:
        raise RuntimeError("Revision does not belong to this message")
    if message.author_type != AuthorType.PLAYER or message.execution_id is None:
        raise RuntimeError("Only generated player declarations can be restored")

    with transaction.atomic():
        locked = Message.objects.select_for_update().get(pk=message.pk)
        locked.content = revision.content
        locked.action_type = revision.action_type
        locked.save(update_fields=["content", "action_type"])
        TurnExecution.objects.filter(pk=locked.execution_id).update(
            action_type=revision.action_type
        )
        _record_message_revision(locked, reason="RESTORE")
    return locked


def undo_latest_public_turn(scene: Scene) -> None:
    with transaction.atomic():
        locked_scene = Scene.objects.select_for_update().get(pk=scene.pk)
        if locked_scene.is_closed:
            raise RuntimeError("Cannot undo in a closed scene")
        latest = (
            Turn.objects.select_for_update()
            .filter(scene=locked_scene)
            .order_by("-created_at", "-pk")
            .first()
        )
        if latest is None:
            raise RuntimeError("There is no turn to undo")
        if latest.mode == TurnMode.ROUND and not latest.is_private and latest.round_advanced:
            order = list(locked_scene.round_order or [])
            if latest.active_player_id_snapshot in order:
                locked_scene.active_player_index = order.index(
                    latest.active_player_id_snapshot
                )
                locked_scene.save(
                    update_fields=["active_player_index", "updated_at"]
                )

        # A persistent external chat cannot forget an undone exchange. Force a
        # fresh authoritative bootstrap next time so application canon and chat
        # memory converge again.
        turn_player_ids = list(
            latest.executions.values_list("player_id", flat=True)
        )
        manual_memory_player_ids = list(
            latest.executions.filter(
                transport=PlayerTransport.MANUAL_CHAT,
                external_context_mode=ManualChatContextMode.CHAT_MEMORY,
            ).values_list("player_id", flat=True)
        )
        if manual_memory_player_ids:
            Player.objects.filter(pk__in=manual_memory_player_ids).update(
                manual_chat_initialized=False
            )
        if turn_player_ids:
            Player.objects.filter(pk__in=turn_player_ids).update(
                status=PlayerStatus.IDLE
            )

        Message.objects.filter(turn=latest).delete()
        latest.delete()


def _refresh_turn_state(turn: Turn) -> None:
    executions = list(turn.executions.all())
    if not executions:
        state = TurnState.COMPLETED
        error = ""
    elif any(
        execution.state in (ExecutionState.FAILED, ExecutionState.INVALID)
        for execution in executions
    ):
        state = TurnState.FAILED
        error = "; ".join(
            f"{execution.player.display_name}: {execution.error}"
            for execution in executions
            if execution.state in (ExecutionState.FAILED, ExecutionState.INVALID)
            and execution.error
        )
    elif all(execution.state == ExecutionState.COMPLETED for execution in executions):
        state = TurnState.COMPLETED
        error = ""
    else:
        state = TurnState.RUNNING
        error = ""

    Turn.objects.filter(pk=turn.pk).update(state=state, error=error)


def _advance_round_once(turn: Turn) -> None:
    with transaction.atomic():
        locked_turn = Turn.objects.select_for_update().get(pk=turn.pk)
        if locked_turn.round_advanced:
            return
        scene = Scene.objects.select_for_update().get(pk=locked_turn.scene_id)
        order = list(scene.round_order or [])
        if order:
            scene.active_player_index = (scene.active_player_index + 1) % len(order)
            scene.save(update_fields=["active_player_index", "updated_at"])
        locked_turn.round_advanced = True
        locked_turn.save(update_fields=["round_advanced", "updated_at"])


def _validated_round_players(scene: Scene) -> list[Player]:
    scene.full_clean()
    order_ids = list(scene.round_order or [])
    if not order_ids:
        raise ValidationError("ROUND mode requires a confirmed player order")
    participant_ids = list(
        scene.scene_participants.order_by("order", "pk")
        .values_list("player_id", flat=True)
    )
    if set(order_ids) != set(participant_ids) or len(order_ids) != len(participant_ids):
        raise ValidationError(
            "ROUND order must contain every scene participant exactly once"
        )
    players_by_id = {
        player.pk: player
        for player in Player.objects.filter(
            scene_participations__scene=scene,
            pk__in=order_ids,
        )
    }
    if len(players_by_id) != len(order_ids):
        raise ValidationError("round_order contains missing or foreign scene participants")
    return [players_by_id[player_id] for player_id in order_ids]


def _validate_selected_players(scene: Scene, players: list[Player]) -> list[Player]:
    if not players:
        return []
    ids = [player.pk for player in players]
    if len(ids) != len(set(ids)):
        raise ValidationError("Duplicate selected players are not allowed")
    valid = {
        player.pk: player
        for player in Player.objects.filter(
            scene_participations__scene=scene,
            pk__in=ids,
        )
    }
    if len(valid) != len(ids):
        raise ValidationError("Selected player is not a participant in this scene")
    return [valid[player_id] for player_id in ids]


def _ensure_scene_participant(scene: Scene, player: Player) -> None:
    if not scene.scene_participants.filter(player=player).exists():
        raise ValidationError("Player is not a participant in this scene")


def _scene_players(scene: Scene) -> list[Player]:
    return list(
        Player.objects.filter(scene_participations__scene=scene)
        .select_related("model_config")
        .order_by("scene_participations__order", "scene_participations__pk")
    )


def _player_model(player: Player) -> str:
    if player.model_config and player.model_config.enabled:
        return player.model_config.gateway_model
    return "mock-echo"


def _player_temp(player: Player) -> float:
    return player.model_config.temperature if player.model_config else 0.7


def _set_player_status(player: Player, status: str) -> None:
    Player.objects.filter(pk=player.pk).update(status=status)


def _normalize_client_turn_id(
    value: str | uuid.UUID | None,
) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("Invalid client_turn_id") from exc
