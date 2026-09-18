"""Turn orchestration for MRAZ Master.

Only explicit GM actions enter this service. Persisting an AI Message never
starts another model call.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from rpg.models import (
    AuthorType,
    ExecutionState,
    Message,
    Player,
    PlayerStatus,
    Scene,
    Turn,
    TurnExecution,
    TurnMode,
    TurnState,
    Visibility,
)
from rpg.services.context_builder import build_player_context, get_player_history_messages
from rpg.services.llm import LLMResponse, get_llm_client

logger = logging.getLogger("rpg.turn_engine")


@dataclass
class TurnResult:
    turn: Turn
    messages: list[Message]


def start_turn(
    *,
    scene: Scene,
    gm_message_text: str,
    selected_players: list[Player] | None = None,
    private_to_player: Player | None = None,
    client_turn_id: str | uuid.UUID | None = None,
) -> TurnResult:
    """Create and execute one immutable GM-triggered turn.

    Private turns are independent from the public scene mode and never advance
    ROUND state. A repeated client_turn_id is idempotent and returns the
    existing turn without re-running model calls.
    """
    if not gm_message_text.strip():
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

        visibility = (
            Visibility.PRIVATE_GM_PLAYER if is_private else Visibility.PUBLIC
        )
        gm_message = Message.objects.create(
            campaign=locked_scene.campaign,
            scene=locked_scene,
            turn=turn,
            author_type=AuthorType.GM,
            content=gm_message_text.strip(),
            visibility=visibility,
            private_player=private_to_player if is_private else None,
        )
        turn.trigger_message = gm_message
        turn.save(update_fields=["trigger_message"])

        executions = [
            TurnExecution.objects.create(
                turn=turn,
                player=player,
                order_index=index,
            )
            for index, player in enumerate(targets)
        ]

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

    client = get_llm_client()
    generated: list[Message] = []

    for execution in executions:
        if mode == TurnMode.TABLE and not is_private:
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


def retry_execution(execution: TurnExecution) -> TurnResult:
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
    message = _run_execution(
        execution=execution,
        client=get_llm_client(),
        out_of_turn=out_of_turn,
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


def _run_execution(
    *,
    execution: TurnExecution,
    client,
    out_of_turn: bool,
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
        context = build_player_context(
            player=player,
            scene=scene,
            trigger_message=turn.trigger_message,
            history=history,
        )

        if out_of_turn:
            context.system_prompt += (
                "\n\n# ROUND ROLE\n"
                "You are NOT the active player this round. "
                "You may only PASS or ACT_OUT_OF_TURN."
            )
        elif turn.mode == TurnMode.ROUND and not turn.is_private:
            context.system_prompt += (
                "\n\n# ROUND ROLE\n"
                "You are the active player this round. You may only ACT or PASS."
            )

        response = client.generate(
            system_prompt=context.system_prompt,
            messages=context.messages,
            model=_player_model(player),
            temperature=_player_temp(player),
        )
        _validate_action(turn=turn, out_of_turn=out_of_turn, response=response)

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
    except InvalidActionError as exc:
        logger.info("Invalid action from %s: %s", player.display_name, exc)
        TurnExecution.objects.filter(pk=execution.pk).update(
            state=ExecutionState.INVALID,
            error=str(exc),
        )
        _set_player_status(player, PlayerStatus.ERROR)
    except Exception as exc:
        logger.warning("LLM call failed for player %s: %s", player.display_name, exc)
        TurnExecution.objects.filter(pk=execution.pk).update(
            state=ExecutionState.FAILED,
            error=str(exc),
        )
        _set_player_status(player, PlayerStatus.ERROR)
    return None


class InvalidActionError(ValueError):
    pass


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
