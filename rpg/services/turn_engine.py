"""Turn Engine.

The central orchestrator. Four modes:
  - MANUAL        : Master selects players; only they respond.
  - ROUND         : ordered round; active player ACTs, others PASS/ACT_OUT_OF_TURN.
  - SIMULTANEOUS  : all selected players see the same public snapshot (no later
                    answers visible).
  - TABLE         : sequential; each player sees previous players' public
                    answers from this turn.

CRITICAL INVARIANT:
  Saving an AI Message NEVER initiates a new LLM call / Turn.
  Only an explicit Master action (a view call into the Turn Engine) starts a turn.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable

from django.db import transaction
from django.utils import timezone

from rpg.models import (
    AuthorType,
    Message,
    Player,
    PlayerStatus,
    Scene,
    Turn,
    TurnMode,
    TurnState,
    Visibility,
)
from rpg.services.context_builder import build_player_context
from rpg.services.llm import get_llm_client, LLMResponse

logger = logging.getLogger("rpg.turn_engine")


@dataclass
class TurnResult:
    turn: Turn
    messages: list[Message]


# ---------------------------------------------------------------------------
# Public entrypoints (called only from views / management commands)
# ---------------------------------------------------------------------------
def start_turn(
    *,
    scene: Scene,
    gm_message_text: str,
    selected_players: list[Player] | None = None,
    private_to_player: Player | None = None,
) -> TurnResult:
    """Start a new turn triggered by a GM message.

    `private_to_player` (optional) makes the GM message PRIVATE_GM_PLAYER to
    that player instead of PUBLIC. Used for the private GM-player channel.

    IMPORTANT: this is the ONLY function that initiates LLM calls.
    """
    client = get_llm_client()

    with transaction.atomic():
        scene = (
            Scene.objects.select_for_update()
            .select_related("campaign")
            .get(pk=scene.pk)
        )
        turn = Turn.objects.create(
            scene=scene,
            mode=scene.mode,
            state=TurnState.RUNNING,
        )

        # Persist the GM trigger message (PUBLIC by default, or PRIVATE).
        if private_to_player is not None:
            gm_msg = Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                turn=turn,
                author_type=AuthorType.GM,
                content=gm_message_text,
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=private_to_player,
            )
        else:
            gm_msg = Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                turn=turn,
                author_type=AuthorType.GM,
                content=gm_message_text,
                visibility=Visibility.PUBLIC,
            )
        turn.trigger_message = gm_msg
        turn.save(update_fields=["trigger_message"])

    # Dispatch by mode. Each returns list[Message].
    mode = scene.mode
    if mode == TurnMode.MANUAL:
        msgs = _run_manual(scene, turn, gm_msg, selected_players or [], client)
    elif mode == TurnMode.ROUND:
        msgs = _run_round(scene, turn, gm_msg, client)
    elif mode == TurnMode.SIMULTANEOUS:
        targets = selected_players or _scene_players(scene)
        msgs = _run_simultaneous(scene, turn, gm_msg, targets, client)
    elif mode == TurnMode.TABLE:
        order = selected_players or _ordered_players(scene)
        msgs = _run_table(scene, turn, gm_msg, order, client)
    else:
        turn.state = TurnState.FAILED
        turn.error = f"Unknown mode {mode}"
        turn.save()
        return TurnResult(turn, [])

    _finalize_turn(turn, msgs)
    return TurnResult(turn, msgs)


def retry_turn(turn: Turn) -> TurnResult:
    """Re-run a FAILED turn from scratch (explicit Master action)."""
    if turn.state not in (TurnState.FAILED,):
        raise RuntimeError("Can only retry a FAILED turn")
    client = get_llm_client()
    with transaction.atomic():
        turn = Turn.objects.select_for_update().get(pk=turn.pk)
        turn.state = TurnState.RUNNING
        turn.error = ""
        turn.save(update_fields=["state", "error"])
    gm_msg = turn.trigger_message
    scene = turn.scene
    # Rebuild based on mode
    mode = turn.mode
    if mode == TurnMode.MANUAL:
        # use original participants
        targets = _participants_as_players(scene, turn.participants)
        msgs = _run_manual(scene, turn, gm_msg, targets, client)
    elif mode == TurnMode.ROUND:
        msgs = _run_round(scene, turn, gm_msg, client)
    elif mode == TurnMode.SIMULTANEOUS:
        targets = _participants_as_players(scene, turn.participants)
        msgs = _run_simultaneous(scene, turn, gm_msg, targets, client)
    elif mode == TurnMode.TABLE:
        order = _participants_as_players(scene, turn.participants)
        msgs = _run_table(scene, turn, gm_msg, order, client)
    else:
        msgs = []
    _finalize_turn(turn, msgs)
    return TurnResult(turn, msgs)


# ---------------------------------------------------------------------------
# Mode runners
# ---------------------------------------------------------------------------
def _run_manual(scene, turn, gm_msg, players: list[Player], client) -> list[Message]:
    """Only selected players respond. No automatic continuation."""
    if not players:
        return []
    targets = _ordered_targets(players)
    return _generate_for_players(scene, turn, gm_msg, targets, client, snapshot_history=None)


def _run_round(scene, turn, gm_msg, client) -> list[Message]:
    """Active player gets a normal ACT; others may PASS/ACT_OUT_OF_TURN.

    After the turn, advance active_player_index by exactly one position.
    """
    order_ids: list[int] = list(scene.round_order or [])
    if not order_ids:
        return []
    idx = scene.active_player_index % len(order_ids)
    active_id = order_ids[idx]
    players_by_id = {p.pk: p for p in Player.objects.filter(pk__in=order_ids)}
    # ordered
    ordered_players = [players_by_id[pid] for pid in order_ids if pid in players_by_id]

    results: list[Message] = []
    for p in ordered_players:
        is_active = (p.pk == active_id)
        # For non-active, we still call the model but instruct it may PASS.
        msgs = _generate_for_players(scene, turn, gm_msg, [p], client,
                                      snapshot_history=None,
                                      out_of_turn=(not is_active))
        results.extend(msgs)

    # Advance active player exactly one position (cyclic).
    with transaction.atomic():
        scene = Scene.objects.select_for_update().get(pk=scene.pk)
        scene.active_player_index = (idx + 1) % len(order_ids)
        scene.save(update_fields=["active_player_index", "updated_at"])

    return results


def _run_simultaneous(scene, turn, gm_msg, players: list[Player], client) -> list[Message]:
    """All targets see the SAME public snapshot (before any of their answers)."""
    snapshot = list(
        Message.objects.filter(scene=scene)
        .select_related("author_player")
        .order_by("created_at")
    )
    targets = _ordered_targets(players)
    turn.participants = [p.pk for p in targets]
    turn.save(update_fields=["participants"])
    return _generate_for_players(scene, turn, gm_msg, targets, client,
                                  snapshot_history=snapshot)


def _run_table(scene, turn, gm_msg, order: list[Player], client) -> list[Message]:
    """Sequential: each player sees previous players' public answers this turn."""
    targets = _ordered_targets(order)
    turn.participants = [p.pk for p in targets]
    turn.save(update_fields=["participants"])

    results: list[Message] = []
    # Each new player message is committed before the next, so the next call's
    # history query includes it. snapshot_history=None -> uses live DB.
    for p in targets:
        msgs = _generate_for_players(scene, turn, gm_msg, [p], client, snapshot_history=None)
        results.extend(msgs)
    return results


# ---------------------------------------------------------------------------
# Core generation helper
# ---------------------------------------------------------------------------
def _generate_for_players(
    scene: Scene,
    turn: Turn,
    trigger_msg: Message,
    players: list[Player],
    client,
    *,
    snapshot_history: list[Message] | None,
    out_of_turn: bool = False,
) -> list[Message]:
    """Call LLM for each player in `players` and persist responses.

    snapshot_history != None: use that frozen list for all players (SIMULTANEOUS).
    snapshot_history == None: re-query DB per player (TABLE / MANUAL / ROUND),
      so each sees the latest committed public history.

    IMPORTANT: this function ONLY saves messages. It NEVER starts a new turn.
    """
    results: list[Message] = []
    for p in players:
        _set_player_status(p, PlayerStatus.GENERATING)
        try:
            history = snapshot_history  # may be None
            ctx = build_player_context(
                player=p, scene=scene, trigger_message=trigger_msg, history=history
            )
            model = _player_model(p)
            temp = _player_temp(p)
            if out_of_turn:
                # nudge prompt: you may only PASS or ACT_OUT_OF_TURN
                extra = (
                    "\n\n# NOTE\nYou are NOT the active player this round. "
                    "You may only PASS or ACT_OUT_OF_TURN."
                )
                ctx.system_prompt += extra

            resp: LLMResponse = _run_async(
                client.generate(
                    system_prompt=ctx.system_prompt,
                    messages=ctx.messages,
                    model=model,
                    temperature=temp,
                )
            )
            msg = _persist_player_response(scene, turn, p, resp)
            results.append(msg)
            _set_player_status(p, PlayerStatus.IDLE)
        except Exception as e:
            logger.warning("LLM call failed for player %s: %s", p.display_name, e)
            _set_player_status(p, PlayerStatus.ERROR)
            turn.error = f"{p.display_name}: {e}"
            turn.save(update_fields=["error"])
    return results


def _persist_player_response(
    scene: Scene, turn: Turn, player: Player, resp: LLMResponse
) -> Message:
    """Persist a player response as messages.

    - A PUBLIC message with the `public` content.
    - A separate PRIVATE_GM_PLAYER message with `private_to_gm` (only if non-empty)
      visible to GM + this player only.

    SAVING THIS MESSAGE DOES NOT TRIGGER ANY LLM CALL.
    (Regression-tested in tests/test_no_auto_trigger.py.)
    """
    action = resp.action_type or "ACT"
    with transaction.atomic():
        public_msg = Message.objects.create(
            campaign=scene.campaign,
            scene=scene,
            turn=turn,
            author_type=AuthorType.PLAYER,
            author_player=player,
            content=resp.public or f"[{action}] {player.display_name}",
            visibility=Visibility.PUBLIC,
            action_type=action,
            private_to_gm=resp.private_to_gm,
        )
        if resp.private_to_gm and resp.private_to_gm.strip():
            Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                turn=turn,
                author_type=AuthorType.PLAYER,
                author_player=player,
                content=resp.private_to_gm,
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
                action_type=action,
            )
    return public_msg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _finalize_turn(turn: Turn, msgs: list[Message]) -> None:
    with transaction.atomic():
        turn.refresh_from_db()
        if turn.error and not msgs:
            turn.state = TurnState.FAILED
        elif turn.error:
            # partial failure
            turn.state = TurnState.FAILED
        else:
            turn.state = TurnState.COMPLETED
        turn.save(update_fields=["state"])


def _ordered_targets(players: list[Player]) -> list[Player]:
    # preserve given order (or created order)
    return list(players)


def _scene_players(scene: Scene) -> list[Player]:
    return list(Player.objects.filter(campaign=scene.campaign).order_by("created_at"))


def _ordered_players(scene: Scene) -> list[Player]:
    order_ids: list[int] = list(scene.round_order or [])
    players_by_id = {p.pk: p for p in Player.objects.filter(pk__in=order_ids)}
    return [players_by_id[pid] for pid in order_ids if pid in players_by_id]


def _participants_as_players(scene: Scene, ids: list[int]) -> list[Player]:
    players_by_id = {p.pk: p for p in Player.objects.filter(pk__in=ids)}
    return [players_by_id[i] for i in ids if i in players_by_id]


def _player_model(player: Player) -> str:
    if player.model_config and player.model_config.enabled:
        return player.model_config.gateway_model
    # fallback
    return "mock-echo"


def _player_temp(player: Player) -> float:
    if player.model_config:
        return player.model_config.temperature
    return 0.7


def _set_player_status(player: Player, status: str) -> None:
    Player.objects.filter(pk=player.pk).update(status=status)


def _run_async(coro):
    """Run a coroutine synchronously with a fresh event loop (Py 3.13 safe)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()