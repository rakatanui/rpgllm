"""Views for MRAZ Master. Business rules live in services."""
import mimetypes
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from rpg.forms import CharacterImageUploadForm
from rpg.models import (
    AuthorType,
    Campaign,
    CharacterAppearance,
    LoreEntry,
    LoreScope,
    Message,
    MessageRevision,
    ModelConfig,
    Player,
    PlayerTransport,
    Scene,
    SceneParticipant,
    ExecutionState,
    GameMasterConfig,
    GameMasterExecution,
    GameMasterExecutionState,
    TurnExecution,
    TurnMode,
    TurnState,
    Visibility,
)
from rpg.services import gm_engine, turn_engine
from rpg.services.context_builder import get_scene_lineage
from rpg.services.scene_summary import generate_close_summary


def _scene_players(scene):
    return list(
        Player.objects.filter(scene_participations__scene=scene)
        .select_related("model_config", "fallback_model_config")
        .order_by("scene_participations__order", "scene_participations__pk")
    )


def _validate_round_order(scene, order):
    """Validate a confirmed ROUND order against this scene's participants."""
    player_ids = list(
        scene.scene_participants.order_by("order", "pk")
        .values_list("player_id", flat=True)
    )
    if not player_ids:
        raise ValidationError("ROUND mode requires at least one scene participant.")
    if not order:
        raise ValidationError("Choose and confirm the ROUND player order first.")
    if len(order) != len(set(order)):
        raise ValidationError("ROUND player order contains duplicate players.")
    if len(order) != len(player_ids) or set(order) != set(player_ids):
        raise ValidationError(
            "ROUND player order must include every scene participant exactly once."
        )
    return order


def _round_order_from_request(request, scene):
    raw_ids = request.POST.getlist("round_order")
    if not raw_ids:
        return _validate_round_order(scene, list(scene.round_order or []))
    try:
        order = [int(value) for value in raw_ids]
    except ValueError as exc:
        raise ValidationError("Invalid player id in ROUND order.") from exc
    return _validate_round_order(scene, order)


def _round_order_is_ready(scene):
    try:
        _validate_round_order(scene, list(scene.round_order or []))
    except ValidationError:
        return False
    return 0 <= scene.active_player_index < len(scene.round_order)


def _active_round_player_id(scene):
    order = list(scene.round_order or [])
    if scene.mode != TurnMode.ROUND or not order:
        return None
    if not (0 <= scene.active_player_index < len(order)):
        return None
    return order[scene.active_player_index]


def _player_color_classes(players):
    return {
        player.pk: f"speaker-player-{index % 6}"
        for index, player in enumerate(players)
    }


def _public_message_blocks(public_messages):
    blocks = []
    current_key = None
    current = None

    for message in public_messages:
        # A real turn is one visual block: GM trigger + every public player reply.
        # Standalone/imported messages remain individually readable instead of
        # collapsing all turn-less archive material into one giant slab.
        key = ("turn", message.turn_id) if message.turn_id else ("message", message.pk)
        if key != current_key:
            current = {
                "key": key,
                "turn_id": message.turn_id,
                "messages": [],
                "tone": "round-block-alt" if len(blocks) % 2 else "round-block-base",
            }
            blocks.append(current)
            current_key = key
        current["messages"].append(message)

    return blocks


_SAOOT_MARKER_RE = re.compile(
    r"\[\[SAOOT:(\d+)\|([^\]]+)\]\](.*?)\[\[/SAOOT\]\]",
    flags=re.DOTALL,
)


def _saoot_candidates(scene):
    latest_round = (
        scene.turns.filter(mode=TurnMode.ROUND, is_private=False)
        .order_by("-created_at", "-pk")
        .first()
    )
    if latest_round is None:
        return []
    return list(
        Message.objects.filter(
            turn=latest_round,
            visibility=Visibility.PUBLIC,
            author_type=AuthorType.PLAYER,
            action_type="ACT_OUT_OF_TURN",
            author_player__isnull=False,
        )
        .select_related("author_player")
        .order_by("created_at", "pk")
    )


def _validate_saoot_markup(content, candidates):
    marker_count = content.count("[[SAOOT:")
    close_count = content.count("[[/SAOOT]]")
    matches = list(_SAOOT_MARKER_RE.finditer(content))

    if marker_count != len(matches) or close_count != len(matches):
        raise ValidationError("Malformed SAOOT marker in GM message.")

    if not matches:
        return

    by_player_id = {
        message.author_player_id: message.author_player
        for message in candidates
        if message.author_player_id
    }
    for match in matches:
        player_id = int(match.group(1))
        player_name = match.group(2).strip()
        resolution_text = match.group(3).strip()
        player = by_player_id.get(player_id)
        if player is None:
            raise ValidationError(
                "SAOOT target must have declared ACT_OUT_OF_TURN in the latest ROUND."
            )
        if player.display_name != player_name:
            raise ValidationError("SAOOT target name does not match player id.")
        if not resolution_text:
            raise ValidationError("SAOOT resolution text cannot be empty.")


def _failed_execution_by_player(scene):
    latest_turn = scene.turns.order_by("-created_at", "-pk").first()
    if latest_turn is None:
        return {}
    failures = (
        latest_turn.executions.filter(
            state__in=[ExecutionState.FAILED, ExecutionState.INVALID]
        )
        .select_related("player")
        .order_by("order_index", "pk")
    )
    return {execution.player_id: execution for execution in failures}


def _waiting_external_by_player(scene):
    latest_turn = scene.turns.order_by("-created_at", "-pk").first()
    if latest_turn is None:
        return {}
    waiting = (
        latest_turn.executions.filter(state=ExecutionState.WAITING_EXTERNAL)
        .select_related("player")
        .order_by("order_index", "pk")
    )
    return {execution.player_id: execution for execution in waiting}


def _waiting_human_by_player(scene):
    latest_turn = scene.turns.order_by("-created_at", "-pk").first()
    if latest_turn is None:
        return {}
    waiting = (
        latest_turn.executions.filter(state=ExecutionState.WAITING_HUMAN)
        .select_related("player", "turn")
        .order_by("order_index", "pk")
    )
    return {execution.player_id: execution for execution in waiting}


def _unread_private_player_ids(scene):
    return list(
        Message.objects.filter(
            scene=scene,
            visibility=Visibility.PRIVATE_GM_PLAYER,
            author_type=AuthorType.PLAYER,
            gm_unread=True,
            private_player__isnull=False,
        )
        .values_list("private_player_id", flat=True)
        .distinct()
    )


def search_history(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    lineage = get_scene_lineage(scene)
    scene_ids = [item.pk for item in lineage]

    query = (
        Message.objects.filter(scene_id__in=scene_ids)
        .select_related("scene", "author_player", "private_player")
        .order_by("-created_at", "-pk")
    )
    q = (request.GET.get("q") or "").strip()
    author = (request.GET.get("author") or "").strip()
    action = (request.GET.get("action") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()

    if q:
        query = query.filter(content__icontains=q)
    author_player_id = None
    if author == "GM":
        query = query.filter(author_type=AuthorType.GM)
    elif author.startswith("player:"):
        try:
            author_player_id = int(author.split(":", 1)[1])
        except ValueError:
            return HttpResponseBadRequest("invalid author filter")
        query = query.filter(
            author_type=AuthorType.PLAYER,
            author_player_id=author_player_id,
        )
    if action:
        query = query.filter(action_type=action)
    if visibility:
        query = query.filter(visibility=visibility)

    players = list(
        Player.objects.filter(campaign=scene.campaign).order_by("created_at", "pk")
    )
    return render(
        request,
        "rpg/history_search.html",
        {
            "scene": scene,
            "results": list(query[:300]),
            "players": players,
            "q": q,
            "author_filter": author,
            "author_player_id": author_player_id,
            "action_filter": action,
            "visibility_filter": visibility,
        },
    )


def campaigns(request):
    campaigns = Campaign.objects.prefetch_related("scenes").all()
    scenes = Scene.objects.all()
    return render(request, "rpg/campaigns.html", {"campaigns": campaigns, "scenes": scenes})


def scene_view(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    players = _scene_players(scene)
    players_by_id = {player.pk: player for player in players}
    stored_round_order = list(scene.round_order or [])
    round_players = [
        players_by_id[player_id]
        for player_id in stored_round_order
        if player_id in players_by_id
    ]
    round_setup_players = round_players + [
        player for player in players if player.pk not in stored_round_order
    ]
    round_ready = _round_order_is_ready(scene)
    active_round_player = (
        round_players[scene.active_player_index]
        if round_ready and scene.active_player_index < len(round_players)
        else None
    )
    unread_private_player_ids = _unread_private_player_ids(scene)
    human_access_by_player, human_client_url_by_player = _human_access_ui_context(
        scene,
    )
    public_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.PUBLIC)
        .select_related("author_player", "execution")
        .order_by("-created_at", "-pk")
    )
    player_color_by_id = _player_color_classes(players)
    public_blocks = _public_message_blocks(public_messages)
    failed_execution_by_player = _failed_execution_by_player(scene)
    waiting_external_by_player = _waiting_external_by_player(scene)
    waiting_human_by_player = _waiting_human_by_player(scene)
    saoot_candidates = _saoot_candidates(scene)
    gm_only_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.GM_ONLY)
        .order_by("created_at", "pk")
    )
    public_by_execution = {
        message.execution_id: message
        for message in public_messages
        if message.execution_id
        and message.author_type == AuthorType.PLAYER
    }

    gm_config = (
        GameMasterConfig.objects.filter(campaign=scene.campaign)
        .select_related("model_config", "fallback_model_config")
        .first()
    )
    gm_active_execution = gm_engine.get_active_gm_execution(scene)
    gm_latest_execution = gm_engine.get_latest_gm_execution(scene)
    gm_execution = gm_active_execution or gm_latest_execution
    gm_private_drafts = {}
    gm_target_ids = set()
    if gm_execution is not None:
        gm_private_drafts = {
            int(item.get("player_id")): str(item.get("content", ""))
            for item in (gm_execution.private_drafts or [])
            if item.get("player_id") is not None
        }
        gm_target_ids = {
            int(player_id)
            for player_id in (gm_execution.turn_targets or [])
        }

    player_private = {}
    for player in players:
        private_messages = list(
            Message.objects.filter(
                scene=scene,
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
            )
            .select_related(
                "author_player",
                "turn__trigger_message",
                "execution",
            )
            .order_by("-created_at", "-pk")
        )
        player_private[player.pk] = [
            {
                "message": message,
                "public_message": (
                    public_by_execution.get(message.execution_id)
                    if message.execution_id
                    else None
                ),
                "trigger_message": (
                    message.turn.trigger_message
                    if message.turn_id and message.turn.trigger_message_id
                    else None
                ),
            }
            for message in private_messages
        ]
    return render(
        request,
        "rpg/scene.html",
        {
            "scene": scene,
            "campaign": scene.campaign,
            "players": players,
            "public_messages": public_messages,
            "public_blocks": public_blocks,
            "player_color_by_id": player_color_by_id,
            "failed_execution_by_player": failed_execution_by_player,
            "waiting_external_by_player": waiting_external_by_player,
            "waiting_human_by_player": waiting_human_by_player,
            "saoot_candidates": saoot_candidates,
            "gm_only_messages": gm_only_messages,
            "gm_config": gm_config,
            "gm_execution": gm_execution,
            "gm_active_execution": gm_active_execution,
            "gm_private_drafts": gm_private_drafts,
            "gm_target_ids": gm_target_ids,
            "gm_actions": [
                ("TURN", "TURN · publish + call players"),
                ("NARRATE", "NARRATE · publish only"),
                ("WAIT", "WAIT · publish nothing"),
            ],
            "player_private": player_private,
            "modes": TurnMode.choices,
            "round_players": round_players,
            "round_setup_players": round_setup_players,
            "round_ready": round_ready,
            "active_round_player": active_round_player,
            "unread_private_player_ids": unread_private_player_ids,
            "human_access_by_player": human_access_by_player,
            "human_client_url_by_player": human_client_url_by_player,
            "active_round_player_id": _active_round_player_id(scene),
            "previous_scenes": list(
                scene.previous_scenes.order_by("created_at", "pk")
            ),
            "all_campaign_players": list(
                Player.objects.filter(campaign=scene.campaign)
                .order_by("created_at", "pk")
            ),
            "source_participant_ids": [player.pk for player in players],
            "latest_turn": scene.turns.order_by("-created_at", "-pk").first(),
            "close_summary_draft": scene.close_summary_draft or {},
            "close_player_drafts": {
                player.pk: (scene.close_summary_draft or {}).get("players", {}).get(
                    str(player.pk), ""
                )
                for player in players
            },
        },
    )


def scene_fragment(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    return scene_view_fragment(request, scene)


def scene_view_fragment(request, scene):
    players = _scene_players(scene)
    public_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.PUBLIC)
        .select_related("author_player", "execution")
        .order_by("-created_at", "-pk")
    )
    return render(
        request,
        "rpg/_scene_messages.html",
        {
            "scene": scene,
            "players": players,
            "public_messages": public_messages,
            "public_blocks": _public_message_blocks(public_messages),
            "player_color_by_id": _player_color_classes(players),
        },
    )


def players_status(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    players = _scene_players(scene)
    human_access_by_player, human_client_url_by_player = _human_access_ui_context(
        scene,
    )
    return render(
        request,
        "rpg/_players.html",
        {
            "scene": scene,
            "players": players,
            "unread_private_player_ids": _unread_private_player_ids(scene),
            "human_access_by_player": human_access_by_player,
            "human_client_url_by_player": human_client_url_by_player,
            "active_round_player_id": _active_round_player_id(scene),
            "failed_execution_by_player": _failed_execution_by_player(scene),
            "waiting_external_by_player": _waiting_external_by_player(scene),
            "waiting_human_by_player": _waiting_human_by_player(scene),
        },
    )


def _human_access_ui_context(scene: Scene) -> tuple[dict, dict]:
    participations = {
        participation.player_id: participation
        for participation in SceneParticipant.objects.filter(
            scene=scene,
            player__transport=PlayerTransport.HUMAN,
        ).select_related("player")
    }
    public_host = getattr(settings, "PUBLIC_PLAYER_HOST", "")
    urls = {}
    for player_id, participation in participations.items():
        path = reverse(
            "human_player_client",
            kwargs={"access_token": participation.human_access_token},
        )
        urls[player_id] = (
            f"https://{public_host}{path}"
            if public_host
            else path
        )
    return participations, urls


def _human_access_for_token(access_token):
    participation = get_object_or_404(
        SceneParticipant.objects.select_related("scene__campaign", "player"),
        human_access_token=access_token,
        human_access_enabled=True,
        player__transport=PlayerTransport.HUMAN,
    )
    return participation.scene, participation.player, participation


def _secure_human_response(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


HUMAN_FEED_PAGE_SIZE = 60
_HUMAN_EMPTY_MESSAGE_MARKERS = {"none", "null", "undefined"}


def _human_message_has_visible_content(message: Message) -> bool:
    if (message.action_type or "").strip().upper() == "PASS":
        return True
    content = (message.content or "").strip()
    return bool(content and content.casefold() not in _HUMAN_EMPTY_MESSAGE_MARKERS)


def _human_channel_page(
    *,
    scene: Scene,
    player: Player,
    channel: str,
    before_id: int | None = None,
) -> tuple[list[Message], bool, int | None]:
    if channel == "public":
        query = Message.objects.filter(
            scene=scene,
            visibility=Visibility.PUBLIC,
        ).select_related("author_player", "execution")
    elif channel == "private":
        query = Message.objects.filter(
            scene=scene,
            visibility=Visibility.PRIVATE_GM_PLAYER,
            private_player=player,
        ).select_related("author_player", "execution", "turn__trigger_message")
    else:
        raise ValueError(f"Unsupported human feed channel: {channel}")

    if before_id is not None:
        query = query.filter(pk__lt=before_id)

    query = query.order_by("-created_at", "-pk")

    # Fetch a little extra so legacy empty/None-like rows can be discarded without
    # shrinking an ordinary page. The next cursor remains an explicit message id,
    # and older history is always appended below the newest-first live window.
    raw = list(query[: HUMAN_FEED_PAGE_SIZE * 2 + 1])
    visible = [message for message in raw if _human_message_has_visible_content(message)]
    page = visible[:HUMAN_FEED_PAGE_SIZE]

    next_before = page[-1].pk if page else None
    has_older = bool(
        next_before
        and Message.objects.filter(
            scene=scene,
            pk__lt=next_before,
        )
        .filter(
            Q(visibility=Visibility.PUBLIC)
            if channel == "public"
            else Q(
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
            )
        )
        .exists()
    )
    return page, has_older, next_before


def _human_client_context(scene: Scene, player: Player, request=None) -> dict:
    appearances = list(player.appearances.all().order_by("order", "pk"))
    participation = (
        SceneParticipant.objects.filter(scene=scene, player=player)
        .select_related("current_appearance")
        .first()
    )
    current_appearance = participation.current_appearance if participation else None
    if current_appearance is None:
        current_appearance = next((item for item in appearances if item.is_primary), None)
    if current_appearance is None and appearances:
        current_appearance = appearances[0]

    display_appearance = current_appearance
    if request is not None:
        raw_appearance_id = (request.GET.get("appearance") or "").strip()
        if raw_appearance_id.isdigit():
            requested_id = int(raw_appearance_id)
            display_appearance = next(
                (item for item in appearances if item.pk == requested_id),
                display_appearance,
            )

    public_messages, public_has_older, public_next_before = _human_channel_page(
        scene=scene,
        player=player,
        channel="public",
    )
    private_messages, private_has_older, private_next_before = _human_channel_page(
        scene=scene,
        player=player,
        channel="private",
    )

    waiting = (
        TurnExecution.objects.filter(
            turn__scene=scene,
            player=player,
            transport=PlayerTransport.HUMAN,
            state=ExecutionState.WAITING_HUMAN,
        )
        .select_related("turn__trigger_message", "player")
        .order_by("-created_at", "-pk")
        .first()
    )
    allowed_actions = (
        turn_engine.allowed_human_actions(waiting)
        if waiting is not None
        else []
    )
    active_gm_execution = gm_engine.get_active_gm_execution(scene)
    is_active_round = (
        scene.mode == TurnMode.ROUND
        and _active_round_player_id(scene) == player.pk
    )
    return {
        "scene": scene,
        "campaign": scene.campaign,
        "player": player,
        "appearances": appearances,
        "current_appearance": current_appearance,
        "display_appearance": display_appearance,
        "public_messages": public_messages,
        "public_blocks": _public_message_blocks(public_messages),
        "public_latest_id": public_messages[0].pk if public_messages else None,
        "public_has_older": public_has_older,
        "public_next_before": public_next_before,
        "private_messages": private_messages,
        "private_latest_id": private_messages[0].pk if private_messages else None,
        "private_has_older": private_has_older,
        "private_next_before": private_next_before,
        "player_color": _player_color_classes(_scene_players(scene)).get(player.pk, ""),
        "waiting_execution": waiting,
        "gm_active_execution": active_gm_execution,
        "allowed_actions": allowed_actions,
        "is_active_round": is_active_round,
        "access_token": participation.human_access_token if participation else None,
    }


def _human_visible_messages(scene: Scene, player: Player):
    return (
        Message.objects.filter(scene=scene)
        .filter(
            Q(visibility=Visibility.PUBLIC)
            | Q(
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
            )
        )
        .select_related("author_player", "execution", "turn__trigger_message")
        .order_by("-created_at", "-pk")
    )


def human_player_client(request, access_token):
    scene, player, _ = _human_access_for_token(access_token)
    response = render(
        request,
        "rpg/human_player.html",
        _human_client_context(scene, player, request),
    )
    return _secure_human_response(response)


def human_player_fragment(request, access_token):
    scene, player, _ = _human_access_for_token(access_token)
    workspace = (request.GET.get("workspace") or "scene").strip().lower()
    template_by_workspace = {
        "scene": "rpg/_human_player_panel.html",
        "private": "rpg/_human_private_panel.html",
    }
    template_name = template_by_workspace.get(workspace)
    if template_name is None:
        return HttpResponseBadRequest("invalid human workspace")

    response = render(
        request,
        template_name,
        _human_client_context(scene, player, request),
    )
    return _secure_human_response(response)


def human_feed_page(request, access_token, channel):
    scene, player, _ = _human_access_for_token(access_token)
    channel = (channel or "").strip().lower()
    if channel not in {"public", "private"}:
        return HttpResponseBadRequest("invalid human feed channel")

    raw_before = (request.GET.get("before") or "").strip()
    if not raw_before.isdigit():
        return HttpResponseBadRequest("missing or invalid history cursor")

    messages, has_older, next_before = _human_channel_page(
        scene=scene,
        player=player,
        channel=channel,
        before_id=int(raw_before),
    )
    context = {
        "scene": scene,
        "player": player,
        "access_token": access_token,
        "channel": channel,
        "has_older": has_older,
        "next_before": next_before,
    }
    if channel == "public":
        context["public_blocks"] = _public_message_blocks(messages)
    else:
        context["private_messages"] = messages

    response = render(request, "rpg/_human_feed_page.html", context)
    return _secure_human_response(response)


@csrf_exempt
@require_http_methods(["POST"])
def submit_human_response(request, access_token, execution_id):
    scene, player, _ = _human_access_for_token(access_token)
    execution = get_object_or_404(
        TurnExecution.objects.select_related("turn__scene", "player"),
        pk=execution_id,
        player=player,
        turn__scene=scene,
        transport=PlayerTransport.HUMAN,
    )
    try:
        result = turn_engine.submit_human_response(
            execution=execution,
            action_type=request.POST.get("action_type") or "",
            public_text=request.POST.get("content") or "",
            private_to_gm=request.POST.get("private_to_gm") or "",
        )
        if (
            result.turn.state == TurnState.COMPLETED
            and not result.turn.is_private
        ):
            gm_engine.maybe_start_auto_gm(scene=scene)
    except ValidationError:
        # Keep the player on the client page; the execution stores the rejection
        # and the polling panel displays it above the preserved draft.
        return redirect(
            reverse(
                "human_player_client",
                kwargs={"access_token": access_token},
            )
        )

    return redirect(
        reverse(
            "human_player_client",
            kwargs={"access_token": access_token},
        )
    )


@csrf_exempt
@require_http_methods(["POST"])
def human_send_ooc(request, access_token):
    scene, player, _ = _human_access_for_token(access_token)
    if scene.is_closed:
        return HttpResponseBadRequest("scene is closed and read-only")
    content = (request.POST.get("content") or "").strip()
    if not content:
        return HttpResponseBadRequest("empty OOC content")

    Message.objects.create(
        campaign=scene.campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=player,
        content=f"[OOC PLAYER]\n{content}",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=player,
        gm_unread=True,
    )
    return redirect(
        reverse(
            "human_player_client",
            kwargs={"access_token": access_token},
        )
    )


def human_appearance_image(request, access_token, appearance_id, image_kind):
    _, player, _ = _human_access_for_token(access_token)
    appearance = get_object_or_404(
        CharacterAppearance,
        pk=appearance_id,
        player=player,
    )
    if image_kind == "portrait":
        image = appearance.portrait_image
    elif image_kind == "fullbody":
        image = appearance.fullbody_image
    else:
        return HttpResponse(status=404)
    if not image:
        return HttpResponse(status=404)

    content_type = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
    response = FileResponse(image.open("rb"), content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@csrf_exempt
@require_http_methods(["POST"])
def set_human_current_appearance(request, access_token, appearance_id):
    scene, player, participation = _human_access_for_token(access_token)
    if scene.is_closed:
        return HttpResponseBadRequest("scene is closed and read-only")
    appearance = get_object_or_404(CharacterAppearance, pk=appearance_id, player=player)
    participation.current_appearance = appearance
    participation.full_clean()
    participation.save(update_fields=["current_appearance"])
    return redirect(
        reverse(
            "human_player_client",
            kwargs={"access_token": access_token},
        )
        + f"?appearance={appearance.pk}"
    )


def human_character_image(request, access_token):
    _, player, _ = _human_access_for_token(access_token)
    if not player.character_image:
        return HttpResponse(status=404)

    content_type = mimetypes.guess_type(player.character_image.name)[0] or "application/octet-stream"
    response = FileResponse(player.character_image.open("rb"), content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@csrf_exempt
@require_http_methods(["POST"])
def upload_human_character_image(request, access_token):
    _, player, _ = _human_access_for_token(access_token)
    form = CharacterImageUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return HttpResponseBadRequest(form.errors.as_text())

    old_name = player.character_image.name if player.character_image else ""
    player.character_image = form.cleaned_data["image"]
    player.save(update_fields=["character_image", "updated_at"])
    if old_name and old_name != player.character_image.name:
        player.character_image.storage.delete(old_name)

    return redirect(
        reverse(
            "human_player_client",
            kwargs={"access_token": access_token},
        )
    )


@csrf_exempt
@require_http_methods(["POST"])
def remove_human_character_image(request, access_token):
    _, player, _ = _human_access_for_token(access_token)
    if player.character_image:
        storage = player.character_image.storage
        old_name = player.character_image.name
        player.character_image = ""
        player.save(update_fields=["character_image", "updated_at"])
        storage.delete(old_name)

    return redirect(
        reverse(
            "human_player_client",
            kwargs={"access_token": access_token},
        )
    )


def human_episode_search(request, access_token):
    current_scene, player, _ = _human_access_for_token(access_token)
    q = (request.GET.get("q") or "").strip()

    episodes = (
        Scene.objects.filter(
            campaign=player.campaign,
            scene_participants__player=player,
            created_at__lte=current_scene.created_at,
        )
        .distinct()
        .order_by("-created_at", "-pk")
    )

    if q:
        visible_message_match = Q(messages__content__icontains=q) & (
            Q(messages__visibility=Visibility.PUBLIC)
            | Q(
                messages__visibility=Visibility.PRIVATE_GM_PLAYER,
                messages__private_player=player,
            )
        )
        episodes = episodes.filter(
            Q(name__icontains=q)
            | Q(description__icontains=q)
            | Q(memory_summary__icontains=q)
            | visible_message_match
        ).distinct()

    response = render(
        request,
        "rpg/human_episode_search.html",
        {
            "scene": current_scene,
            "campaign": current_scene.campaign,
            "player": player,
            "episodes": list(episodes[:200]),
            "q": q,
            "access_token": access_token,
        },
    )
    return _secure_human_response(response)


def human_episode_detail(request, access_token, episode_id):
    current_scene, player, _ = _human_access_for_token(access_token)
    episode = get_object_or_404(
        Scene.objects.select_related("campaign"),
        pk=episode_id,
        campaign=player.campaign,
        scene_participants__player=player,
        created_at__lte=current_scene.created_at,
    )

    visible_messages = list(_human_visible_messages(episode, player))
    public_messages = [
        message
        for message in visible_messages
        if message.visibility == Visibility.PUBLIC
    ]
    private_messages = [
        message
        for message in visible_messages
        if message.visibility == Visibility.PRIVATE_GM_PLAYER
    ]

    response = render(
        request,
        "rpg/human_episode_detail.html",
        {
            "scene": current_scene,
            "campaign": current_scene.campaign,
            "player": player,
            "episode": episode,
            "public_blocks": _public_message_blocks(public_messages),
            "private_messages": private_messages,
            "access_token": access_token,
        },
    )
    return _secure_human_response(response)


def _selected_players_from_request(request, scene):
    raw_ids = request.POST.getlist("selected_players")
    if not raw_ids:
        return []
    try:
        requested_ids = [int(value) for value in raw_ids]
    except ValueError as exc:
        raise ValidationError("Invalid selected player id") from exc
    if len(requested_ids) != len(set(requested_ids)):
        raise ValidationError("Duplicate selected players are not allowed")
    players_by_id = {
        player.pk: player
        for player in Player.objects.filter(
            scene_participations__scene=scene,
            pk__in=requested_ids,
        )
    }
    if len(players_by_id) != len(requested_ids):
        raise ValidationError("Selected player is not a participant in this scene")
    return [players_by_id[player_id] for player_id in requested_ids]


@require_http_methods(["POST"])
def regenerate_human_access(request, scene_id, player_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    participation = get_object_or_404(
        SceneParticipant.objects.select_related("player"),
        scene=scene,
        player_id=player_id,
        player__transport=PlayerTransport.HUMAN,
    )
    participation.human_access_token = uuid.uuid4()
    participation.human_access_enabled = True
    participation.save(
        update_fields=["human_access_token", "human_access_enabled"]
    )
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def toggle_human_access(request, scene_id, player_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    participation = get_object_or_404(
        SceneParticipant.objects.select_related("player"),
        scene=scene,
        player_id=player_id,
        player__transport=PlayerTransport.HUMAN,
    )
    participation.human_access_enabled = not participation.human_access_enabled
    participation.save(update_fields=["human_access_enabled"])
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def start_model_gm(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    try:
        gm_engine.start_gm_execution(scene=scene)
    except ValidationError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def submit_external_model_gm(request, scene_id, execution_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    execution = get_object_or_404(
        GameMasterExecution.objects.select_related("config", "scene"),
        pk=execution_id,
        scene=scene,
    )
    try:
        gm_engine.submit_external_gm_response(
            execution=execution,
            raw_text=request.POST.get("response") or "",
        )
    except ValidationError:
        # Keep the bridge visible with its stored rejection reason.
        pass
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def publish_model_gm(request, scene_id, execution_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    execution = get_object_or_404(
        GameMasterExecution,
        pk=execution_id,
        scene=scene,
    )

    action = (request.POST.get("action") or execution.action or "").strip().upper()
    public_text = request.POST.get("public")
    private_by_player = {
        player.pk: request.POST.get(f"private_{player.pk}") or ""
        for player in _scene_players(scene)
    }

    raw_target_ids = request.POST.getlist("turn_targets")
    try:
        target_ids = [int(value) for value in raw_target_ids]
    except ValueError:
        return HttpResponseBadRequest("invalid GM turn target")
    if action != "TURN":
        target_ids = []
    if action == "WAIT":
        public_text = ""
        private_by_player = {}

    try:
        gm_engine.publish_gm_execution(
            execution=execution,
            action=action,
            public_text=public_text,
            private_by_player=private_by_player,
            turn_target_ids=target_ids,
        )
    except ValidationError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def discard_model_gm(request, scene_id, execution_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    execution = get_object_or_404(
        GameMasterExecution,
        pk=execution_id,
        scene=scene,
    )
    try:
        gm_engine.discard_gm_execution(execution=execution)
    except ValidationError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def reset_model_gm_chat(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    config = get_object_or_404(GameMasterConfig, campaign=scene.campaign)
    gm_engine.reset_gm_manual_chat_memory(config=config)
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def send_gm_message(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    content = (request.POST.get("content") or "").strip()
    run_turn_flag = request.POST.get("run_turn", "0") == "1"
    if not content:
        return HttpResponseBadRequest("empty content")
    if scene.is_closed:
        return HttpResponseBadRequest("scene is closed and read-only")

    if scene.mode == TurnMode.ROUND and not _round_order_is_ready(scene):
        return HttpResponseBadRequest(
            "ROUND mode requires a confirmed player order before sending messages."
        )

    try:
        saoot_candidates = _saoot_candidates(scene)
        _validate_saoot_markup(content, saoot_candidates)
        selected_players = _selected_players_from_request(request, scene)
        if (
            run_turn_flag
            and scene.mode == TurnMode.MANUAL
            and not selected_players
        ):
            return HttpResponseBadRequest(
                "MANUAL mode requires selecting at least one player."
            )
        if run_turn_flag:
            turn_engine.start_turn(
                scene=scene,
                gm_message_text=content,
                selected_players=selected_players or None,
                client_turn_id=request.POST.get("client_turn_id") or None,
            )
        else:
            Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                author_type=AuthorType.GM,
                content=content,
                visibility=Visibility.PUBLIC,
            )
    except ValidationError as exc:
        return HttpResponseBadRequest(str(exc))

    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def silent_turn(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    if scene.is_closed:
        return HttpResponseBadRequest("scene is closed and read-only")
    if scene.mode not in (TurnMode.ROUND, TurnMode.MANUAL):
        return HttpResponseBadRequest("Silence is available only in ROUND or MANUAL mode.")

    try:
        selected_players = _selected_players_from_request(request, scene)
        if scene.mode == TurnMode.ROUND:
            if not _round_order_is_ready(scene):
                return HttpResponseBadRequest(
                    "ROUND mode requires a confirmed player order before Silence."
                )
            selected_players = []
        elif len(selected_players) != 1:
            return HttpResponseBadRequest(
                "MANUAL Silence requires selecting exactly one player."
            )

        turn_engine.start_silent_turn(
            scene=scene,
            selected_players=selected_players or None,
            client_turn_id=request.POST.get("client_turn_id") or None,
        )
    except ValidationError as exc:
        return HttpResponseBadRequest(str(exc))

    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def send_private_message(request, scene_id, player_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    player = get_object_or_404(
        Player,
        pk=player_id,
        scene_participations__scene=scene,
    )
    content = (request.POST.get("content") or "").strip()
    # Private messages are informational by default. The model is called only
    # when the GM explicitly checks "Ask for response".
    run_turn_flag = request.POST.get("run_turn", "0") == "1"
    if not content:
        return HttpResponseBadRequest("empty content")
    if scene.is_closed:
        return HttpResponseBadRequest("scene is closed and read-only")

    try:
        if run_turn_flag:
            turn_engine.start_turn(
                scene=scene,
                gm_message_text=content,
                selected_players=[player],
                private_to_player=player,
                client_turn_id=request.POST.get("client_turn_id") or None,
            )
        else:
            Message.objects.create(
                campaign=scene.campaign,
                scene=scene,
                author_type=AuthorType.GM,
                content=content,
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
            )
    except ValidationError as exc:
        return HttpResponseBadRequest(str(exc))

    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def mark_private_read(request, scene_id, player_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    player = get_object_or_404(
        Player,
        pk=player_id,
        scene_participations__scene=scene,
    )
    Message.objects.filter(
        scene=scene,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        author_type=AuthorType.PLAYER,
        private_player=player,
        gm_unread=True,
    ).update(gm_unread=False)
    return HttpResponse(status=204)


@require_http_methods(["POST"])
def submit_external_response(request, scene_id, execution_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    execution = get_object_or_404(
        TurnExecution.objects.select_related("turn__scene", "player"),
        pk=execution_id,
        turn__scene=scene,
        transport=PlayerTransport.MANUAL_CHAT,
    )
    raw = request.POST.get("response") or ""
    try:
        turn_engine.submit_external_response(
            execution=execution,
            raw_text=raw,
        )
    except ValidationError:
        # The service stores the rejection reason on the execution. Redirect back
        # so polling/UI shows it next to the same paste box instead of replacing
        # the whole app with a bare HTTP 400 page.
        pass
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def reset_manual_chat_memory(request, scene_id, player_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    player = get_object_or_404(
        Player,
        pk=player_id,
        scene_participations__scene=scene,
        transport=PlayerTransport.MANUAL_CHAT,
    )
    player.manual_chat_initialized = False
    player.save(update_fields=["manual_chat_initialized", "updated_at"])
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def set_mode(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    if scene.is_closed:
        return HttpResponseBadRequest("scene is closed and read-only")
    mode = request.POST.get("mode")
    if mode not in dict(TurnMode.choices):
        return HttpResponseBadRequest("invalid mode")

    try:
        if mode == TurnMode.ROUND:
            scene.round_order = _round_order_from_request(request, scene)
            scene.active_player_index = 0
        scene.mode = mode
        scene.full_clean()
    except ValidationError as exc:
        return HttpResponseBadRequest(str(exc))

    update_fields = ["mode", "updated_at"]
    if mode == TurnMode.ROUND:
        update_fields.extend(["round_order", "active_player_index"])
    scene.save(update_fields=update_fields)
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def retry_execution(request, scene_id, execution_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    execution = get_object_or_404(
        TurnExecution.objects.select_related("turn__scene", "player"),
        pk=execution_id,
        turn__scene=scene,
    )
    if execution.state not in (ExecutionState.FAILED, ExecutionState.INVALID):
        return HttpResponseBadRequest("execution is not retryable")

    try:
        turn_engine.retry_execution(execution)
    except (RuntimeError, ValidationError) as exc:
        return HttpResponseBadRequest(str(exc))

    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def retry_execution_fallback(request, scene_id, execution_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    execution = get_object_or_404(
        TurnExecution.objects.select_related(
            "turn__scene",
            "player__fallback_model_config",
        ),
        pk=execution_id,
        turn__scene=scene,
    )
    fallback = execution.player.fallback_model_config
    if fallback is None or not fallback.enabled:
        return HttpResponseBadRequest("player has no enabled fallback model")
    if execution.state not in (ExecutionState.FAILED, ExecutionState.INVALID):
        return HttpResponseBadRequest("execution is not retryable")
    try:
        turn_engine.retry_execution(execution, model_config=fallback)
    except (RuntimeError, ValidationError) as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def regenerate_execution(request, scene_id, execution_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    execution = get_object_or_404(
        TurnExecution.objects.select_related("turn__scene", "player"),
        pk=execution_id,
        turn__scene=scene,
    )
    if execution.state != ExecutionState.COMPLETED:
        return HttpResponseBadRequest("only a completed execution can be regenerated")

    try:
        turn_engine.regenerate_execution(execution)
    except (RuntimeError, ValidationError) as exc:
        return HttpResponseBadRequest(str(exc))

    return redirect(
        reverse("scene", kwargs={"scene_id": scene.pk})
        + f"#message-{request.POST.get('message_id', '')}"
    )


@require_http_methods(["POST"])
def ooc_revision(request, scene_id, message_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    message = get_object_or_404(
        Message.objects.select_related(
            "author_player",
            "execution__turn__scene",
        ),
        pk=message_id,
        scene=scene,
        visibility=Visibility.PUBLIC,
        author_type=AuthorType.PLAYER,
    )
    comment = (request.POST.get("comment") or "").strip()
    if not comment:
        return HttpResponseBadRequest("OOC comment cannot be empty")

    try:
        turn_engine.revise_execution_ooc(
            public_message=message,
            gm_comment=comment,
        )
    except (RuntimeError, ValidationError) as exc:
        return HttpResponseBadRequest(str(exc))

    return redirect(
        reverse("scene", kwargs={"scene_id": scene.pk})
        + f"#message-{message.pk}"
    )


@require_http_methods(["POST"])
def set_player_nudge(request, scene_id, player_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    player = get_object_or_404(
        Player,
        pk=player_id,
        scene_participations__scene=scene,
    )
    if scene.is_closed:
        return HttpResponseBadRequest("scene is closed and read-only")
    if player.transport == PlayerTransport.HUMAN:
        return HttpResponseBadRequest("Nudge is for model players, not HUMAN transport")
    player.pending_nudge = (request.POST.get("content") or "").strip()
    player.save(update_fields=["pending_nudge", "updated_at"])
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def send_ooc_meta(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    if scene.is_closed:
        return HttpResponseBadRequest("scene is closed and read-only")
    content = (request.POST.get("content") or "").strip()
    if not content:
        return HttpResponseBadRequest("empty OOC content")

    players = _scene_players(scene)
    target = (request.POST.get("target") or "all").strip()
    if target == "all":
        targets = players
    else:
        try:
            target_id = int(target)
        except ValueError:
            return HttpResponseBadRequest("invalid OOC target")
        targets = [player for player in players if player.pk == target_id]
        if not targets:
            return HttpResponseBadRequest("OOC target is not a scene participant")

    for player in targets:
        Message.objects.create(
            campaign=scene.campaign,
            scene=scene,
            author_type=AuthorType.GM,
            content=f"[OOC META]\n{content}",
            visibility=Visibility.PRIVATE_GM_PLAYER,
            private_player=player,
        )
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


def execution_debug(request, scene_id, execution_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    execution = get_object_or_404(
        TurnExecution.objects.select_related("player", "turn"),
        pk=execution_id,
        turn__scene=scene,
    )
    return render(
        request,
        "rpg/execution_debug.html",
        {"scene": scene, "execution": execution},
    )


def message_versions(request, scene_id, message_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    message = get_object_or_404(
        Message.objects.select_related("author_player", "execution"),
        pk=message_id,
        scene=scene,
    )
    try:
        revisions = turn_engine.ensure_message_revision(message)
    except RuntimeError as exc:
        return HttpResponseBadRequest(str(exc))
    return render(
        request,
        "rpg/message_versions.html",
        {"scene": scene, "message": message, "revisions": revisions},
    )


@require_http_methods(["POST"])
def restore_message_version(request, scene_id, message_id, revision_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    message = get_object_or_404(Message, pk=message_id, scene=scene)
    revision = get_object_or_404(
        MessageRevision,
        pk=revision_id,
        message=message,
    )
    try:
        turn_engine.restore_message_revision(message=message, revision=revision)
    except RuntimeError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect(
        reverse("scene", kwargs={"scene_id": scene.pk}) + f"#message-{message.pk}"
    )


@require_http_methods(["POST"])
def undo_latest_turn(request, scene_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    try:
        turn_engine.undo_latest_public_turn(scene)
    except RuntimeError as exc:
        return HttpResponseBadRequest(str(exc))
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


def _append_memory(existing, addition):
    existing = (existing or "").strip()
    addition = (addition or "").strip()
    if not addition:
        return existing
    if addition in existing:
        return existing
    return f"{existing}\n\n{addition}".strip()


@require_http_methods(["POST"])
def pin_message_memory(request, scene_id, message_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    message = get_object_or_404(Message, pk=message_id, scene=scene)
    content = (request.POST.get("content") or "").strip()
    target = (request.POST.get("target") or "").strip()
    if not content:
        return HttpResponseBadRequest("memory text cannot be empty")

    if target == "shared":
        campaign = scene.campaign
        campaign.shared_memory = _append_memory(campaign.shared_memory, content)
        campaign.save(update_fields=["shared_memory", "updated_at"])
    elif target == "scene":
        scene.memory_summary = _append_memory(scene.memory_summary, content)
        scene.save(update_fields=["memory_summary", "updated_at"])
    elif target.startswith("player:"):
        try:
            player_id = int(target.split(":", 1)[1])
        except ValueError:
            return HttpResponseBadRequest("invalid player memory target")
        player = get_object_or_404(
            Player,
            pk=player_id,
            scene_participations__scene=scene,
        )
        player.memory_summary = _append_memory(player.memory_summary, content)
        player.save(update_fields=["memory_summary", "updated_at"])
    elif target == "lore":
        title = (request.POST.get("title") or "").strip() or content[:80]
        lore = LoreEntry.objects.create(
            campaign=scene.campaign,
            title=title,
            category="GM pin",
            content=content,
            scope=LoreScope.SCENE,
            priority=100,
            enabled=True,
        )
        lore.scenes.add(scene)
    else:
        return HttpResponseBadRequest("invalid memory target")
    return redirect(
        reverse("scene", kwargs={"scene_id": scene.pk}) + f"#message-{message.pk}"
    )


@require_http_methods(["POST"])
def prepare_close_summary(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    if scene.is_closed:
        return HttpResponseBadRequest("scene is already closed")
    if scene.turns.filter(state=TurnState.RUNNING).exists():
        return HttpResponseBadRequest("cannot summarize a scene with a running turn")
    try:
        generate_close_summary(scene)
    except Exception as exc:
        return HttpResponseBadRequest(f"summary generation failed: {exc}")
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}) + "?close_review=1")


@require_http_methods(["POST"])
def apply_close_summary(request, scene_id):
    with transaction.atomic():
        scene = get_object_or_404(
            Scene.objects.select_for_update().select_related("campaign"),
            pk=scene_id,
        )
        if scene.is_closed:
            return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))
        if scene.turns.filter(state=TurnState.RUNNING).exists():
            return HttpResponseBadRequest("cannot close a scene with a running turn")

        scene_summary = (request.POST.get("scene_summary") or "").strip()
        open_hooks = (request.POST.get("open_hooks") or "").strip()
        combined = scene_summary
        if open_hooks:
            combined = f"{combined}\n\nOpen hooks:\n{open_hooks}".strip()
        scene.memory_summary = _append_memory(scene.memory_summary, combined)

        players = _scene_players(scene)
        for player in players:
            update = (request.POST.get(f"player_memory_{player.pk}") or "").strip()
            if update:
                player.memory_summary = _append_memory(player.memory_summary, update)
                player.save(update_fields=["memory_summary", "updated_at"])

        scene.close_summary_draft = {}
        scene.is_closed = True
        scene.closed_at = timezone.now()
        scene.save(
            update_fields=[
                "memory_summary",
                "close_summary_draft",
                "is_closed",
                "closed_at",
                "updated_at",
            ]
        )
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def discard_close_summary(request, scene_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    Scene.objects.filter(pk=scene.pk).update(close_summary_draft={})
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def close_scene(request, scene_id):
    with transaction.atomic():
        scene = get_object_or_404(
            Scene.objects.select_for_update().select_related("campaign"),
            pk=scene_id,
        )
        if scene.is_closed:
            return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))
        if scene.turns.filter(state=TurnState.RUNNING).exists():
            return HttpResponseBadRequest("cannot close a scene with a running turn")
        scene.is_closed = True
        scene.closed_at = timezone.now()
        scene.save(update_fields=["is_closed", "closed_at", "updated_at"])

    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def create_followup_scene(request, scene_id):
    source = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    if not source.is_closed:
        return HttpResponseBadRequest("close the source scene before using it as history")

    name = (request.POST.get("name") or "").strip()
    if not name:
        return HttpResponseBadRequest("scene name is required")

    raw_ids = request.POST.getlist("participants")
    if not raw_ids:
        return HttpResponseBadRequest("a follow-up scene needs at least one participant")
    try:
        participant_ids = [int(value) for value in raw_ids]
    except ValueError:
        return HttpResponseBadRequest("invalid participant id")
    if len(participant_ids) != len(set(participant_ids)):
        return HttpResponseBadRequest("duplicate participants")

    valid_players = {
        player.pk: player
        for player in Player.objects.filter(
            campaign=source.campaign,
            pk__in=participant_ids,
        )
    }
    if len(valid_players) != len(participant_ids):
        return HttpResponseBadRequest("all participants must belong to the campaign")
    with transaction.atomic():
        scene = Scene.objects.create(
            campaign=source.campaign,
            name=name,
            mode=TurnMode.MANUAL,
            dialogue_language=source.dialogue_language,
        )
        SceneParticipant.objects.bulk_create(
            [
                SceneParticipant(
                    scene=scene,
                    player=valid_players[player_id],
                    order=index,
                )
                for index, player_id in enumerate(participant_ids)
            ]
        )
        scene.previous_scenes.add(source)

    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


def private_channel(request, scene_id, player_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    player = get_object_or_404(
        Player.objects.select_related("model_config"),
        pk=player_id,
        scene_participations__scene=scene,
    )
    messages = list(
        Message.objects.filter(
            scene=scene,
            visibility=Visibility.PRIVATE_GM_PLAYER,
            private_player=player,
        )
        .select_related("author_player", "turn__trigger_message", "execution")
        .order_by("-created_at", "-pk")
    )
    execution_ids = [m.execution_id for m in messages if m.execution_id]
    public_by_execution = {
        m.execution_id: m
        for m in Message.objects.filter(
            scene=scene,
            visibility=Visibility.PUBLIC,
            execution_id__in=execution_ids,
            author_type=AuthorType.PLAYER,
        )
    }
    entries = [
        {
            "message": message,
            "public_message": public_by_execution.get(message.execution_id),
            "trigger_message": (
                message.turn.trigger_message
                if message.turn_id and message.turn.trigger_message_id
                else None
            ),
        }
        for message in messages
    ]
    players = _scene_players(scene)
    player_color_by_id = _player_color_classes(players)
    return render(
        request,
        "rpg/_private_messages.html",
        {
            "scene": scene,
            "player": player,
            "entries": entries,
            "player_color": player_color_by_id.get(player.pk, ""),
        },
    )


def player_messages(request, player_id, visibility):
    player = get_object_or_404(Player.objects.select_related("campaign"), pk=player_id)
    scene_id = request.GET.get("scene")
    scene = None
    if scene_id:
        scene = get_object_or_404(Scene, pk=scene_id, campaign=player.campaign)
    query = Message.objects.filter(scene=scene, author_player=player)
    if visibility == "private":
        query = query.filter(
            visibility=Visibility.PRIVATE_GM_PLAYER,
            private_player=player,
        )
    elif visibility == "public":
        query = query.filter(visibility=Visibility.PUBLIC)
    messages = list(query.order_by("-created_at", "-pk"))
    return render(
        request,
        "rpg/_player_messages.html",
        {"player": player, "messages": messages, "scene": scene},
    )
