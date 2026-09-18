"""Views for MRAZ Master. Business rules live in services."""
from django.core.exceptions import ValidationError
from django.http import HttpResponseBadRequest
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


def campaigns(request):
    campaigns = Campaign.objects.prefetch_related("scenes").all()
    scenes = Scene.objects.all()
    return render(request, "rpg/campaigns.html", {"campaigns": campaigns, "scenes": scenes})


def scene_view(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    players = list(
        Player.objects.filter(campaign=scene.campaign)
        .select_related("model_config")
        .order_by("created_at", "pk")
    )
    public_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.PUBLIC)
        .select_related("author_player")
        .order_by("created_at", "pk")
    )
    gm_only_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.GM_ONLY)
        .order_by("created_at", "pk")
    )
    player_private = {}
    for player in players:
        player_private[player.pk] = list(
            Message.objects.filter(
                scene=scene,
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=player,
            )
            .select_related("author_player")
            .order_by("created_at", "pk")
        )
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
    return render(request, "rpg/_players.html", {"scene": scene, "players": players})


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

    mode = request.POST.get("mode")
    if mode and mode in dict(TurnMode.choices) and mode != scene.mode:
        scene.mode = mode
        try:
            scene.full_clean()
        except ValidationError as exc:
            return HttpResponseBadRequest(str(exc))
        scene.save(update_fields=["mode", "updated_at"])

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
    run_turn_flag = request.POST.get("run_turn", "1") == "1"
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
def set_mode(request, scene_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    mode = request.POST.get("mode")
    if mode in dict(TurnMode.choices):
        scene.mode = mode
        try:
            scene.full_clean()
        except ValidationError as exc:
            return HttpResponseBadRequest(str(exc))
        scene.save(update_fields=["mode", "updated_at"])
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
