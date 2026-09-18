"""Views for MRAZ Master. Business rules live in services."""
from django.core.exceptions import ValidationError
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from rpg.models import (
    AuthorType,
    Campaign,
    Message,
    Player,
    Scene,
    TurnMode,
    Visibility,
)
from rpg.services import turn_engine


def _campaign_players(scene):
    return list(
        Player.objects.filter(campaign=scene.campaign)
        .select_related("model_config")
        .order_by("created_at", "pk")
    )


def _validate_round_order(scene, order):
    """Validate a confirmed ROUND order against all players in the campaign."""
    player_ids = list(
        Player.objects.filter(campaign=scene.campaign)
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)
    )
    if not player_ids:
        raise ValidationError("ROUND mode requires at least one player.")
    if not order:
        raise ValidationError("Choose and confirm the ROUND player order first.")
    if len(order) != len(set(order)):
        raise ValidationError("ROUND player order contains duplicate players.")
    if len(order) != len(player_ids) or set(order) != set(player_ids):
        raise ValidationError("ROUND player order must include every campaign player exactly once.")
    return order


def _round_order_from_request(request, scene):
    raw_ids = request.POST.getlist("round_order")
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


def campaigns(request):
    campaigns = Campaign.objects.prefetch_related("scenes").all()
    scenes = Scene.objects.all()
    return render(request, "rpg/campaigns.html", {"campaigns": campaigns, "scenes": scenes})


def scene_view(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    players = _campaign_players(scene)
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
    public_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.PUBLIC)
        .select_related("author_player")
        .order_by("created_at", "pk")
    )
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
            .order_by("created_at", "pk")
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
            "gm_only_messages": gm_only_messages,
            "player_private": player_private,
            "modes": TurnMode.choices,
            "round_players": round_players,
            "round_setup_players": round_setup_players,
            "round_ready": round_ready,
            "active_round_player": active_round_player,
            "unread_private_player_ids": unread_private_player_ids,
            "active_round_player_id": _active_round_player_id(scene),
        },
    )


def scene_fragment(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    return scene_view_fragment(request, scene)


def scene_view_fragment(request, scene):
    players = list(
        Player.objects.filter(campaign=scene.campaign).order_by("created_at", "pk")
    )
    public_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.PUBLIC)
        .select_related("author_player")
        .order_by("created_at", "pk")
    )
    return render(
        request,
        "rpg/_scene_messages.html",
        {"scene": scene, "players": players, "public_messages": public_messages},
    )


def players_status(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    players = list(
        Player.objects.filter(campaign=scene.campaign)
        .select_related("model_config")
        .order_by("created_at", "pk")
    )
    return render(
        request,
        "rpg/_players.html",
        {
            "scene": scene,
            "players": players,
            "unread_private_player_ids": _unread_private_player_ids(scene),
            "active_round_player_id": _active_round_player_id(scene),
        },
    )


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
            campaign=scene.campaign,
            pk__in=requested_ids,
        )
    }
    if len(players_by_id) != len(requested_ids):
        raise ValidationError("Selected player does not belong to this campaign")
    return [players_by_id[player_id] for player_id in requested_ids]


@require_http_methods(["POST"])
def send_gm_message(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    content = (request.POST.get("content") or "").strip()
    run_turn_flag = request.POST.get("run_turn", "1") == "1"
    if not content:
        return HttpResponseBadRequest("empty content")

    if scene.mode == TurnMode.ROUND and not _round_order_is_ready(scene):
        return HttpResponseBadRequest(
            "ROUND mode requires a confirmed player order before sending messages."
        )

    try:
        selected_players = _selected_players_from_request(request, scene)
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
def send_private_message(request, scene_id, player_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    player = get_object_or_404(Player, pk=player_id, campaign=scene.campaign)
    content = (request.POST.get("content") or "").strip()
    # Private messages are informational by default. The model is called only
    # when the GM explicitly checks "Ask for response".
    run_turn_flag = request.POST.get("run_turn", "0") == "1"
    if not content:
        return HttpResponseBadRequest("empty content")

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
    player = get_object_or_404(Player, pk=player_id, campaign=scene.campaign)
    Message.objects.filter(
        scene=scene,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        author_type=AuthorType.PLAYER,
        private_player=player,
        gm_unread=True,
    ).update(gm_unread=False)
    return HttpResponse(status=204)


@require_http_methods(["POST"])
def set_mode(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
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
    messages = list(query.order_by("created_at", "pk"))
    return render(
        request,
        "rpg/_player_messages.html",
        {"player": player, "messages": messages, "scene": scene},
    )
