"""Views for MRAZ Master. Thin — no business logic; delegates to services."""
from django.contrib import messages as django_messages
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from rpg.models import Campaign, Message, Player, Scene, TurnMode, AuthorType, Visibility
from rpg.services import turn_engine


def campaigns(request):
    campaigns = Campaign.objects.prefetch_related("scenes").all()
    scenes = Scene.objects.all()
    return render(request, "rpg/campaigns.html", {"campaigns": campaigns, "scenes": scenes})


def scene_view(request, scene_id):
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    players = list(Player.objects.filter(campaign=scene.campaign).order_by("created_at"))
    public_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.PUBLIC).order_by("created_at")
    )
    gm_only_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.GM_ONLY).order_by("created_at")
    )
    # pre-fetch private messages for each player
    player_private = {}
    for p in players:
        player_private[p.pk] = list(
            Message.objects.filter(scene=scene, visibility=Visibility.PRIVATE_GM_PLAYER,
                                    private_player=p).order_by("created_at")
        )
    return render(request, "rpg/scene.html", {
        "scene": scene,
        "campaign": scene.campaign,
        "players": players,
        "public_messages": public_messages,
        "gm_only_messages": gm_only_messages,
        "player_private": player_private,
        "modes": TurnMode.choices,
    })


def scene_fragment(request, scene_id):
    """HTMX partial: returns just the scene messages area."""
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    return scene_view_fragment(request, scene)


def scene_view_fragment(request, scene):
    players = list(Player.objects.filter(campaign=scene.campaign).order_by("created_at"))
    public_messages = list(
        Message.objects.filter(scene=scene, visibility=Visibility.PUBLIC).order_by("created_at")
    )
    return render(request, "rpg/_scene_messages.html", {
        "scene": scene,
        "players": players,
        "public_messages": public_messages,
    })


def players_status(request, scene_id):
    """HTMX partial: player list + status chips."""
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    players = list(Player.objects.filter(campaign=scene.campaign).order_by("created_at"))
    return render(request, "rpg/_players.html", {"scene": scene, "players": players})


@require_http_methods(["POST"])
def send_gm_message(request, scene_id):
    """Master posts a GM message and (optionally) triggers a turn.

    form fields:
      - content: message text (required)
      - mode: current turn mode (optional, defaults to scene.mode)
      - selected_players: list of player IDs (for MANUAL/SIMULTANEOUS/TABLE)
      - run_turn: "1" to actually run the turn (default "1")
    """
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    content = (request.POST.get("content") or "").strip()
    run_turn_flag = request.POST.get("run_turn", "1") == "1"

    if not content:
        return HttpResponseBadRequest("empty content")

    # Optional mode override
    mode = request.POST.get("mode")
    if mode and mode in dict(TurnMode.choices) and mode != scene.mode:
        scene.mode = mode
        scene.save(update_fields=["mode", "updated_at"])

    selected_ids = request.POST.getlist("selected_players")
    selected_players = []
    if selected_ids:
        selected_players = list(Player.objects.filter(pk__in=selected_ids))

    if run_turn_flag:
        # Delegates to Turn Engine (the only initiator of LLM calls).
        turn_engine.start_turn(
            scene=scene,
            gm_message_text=content,
            selected_players=selected_players or None,
        )
    else:
        # Just store a GM message, no LLM call.
        Message.objects.create(
            campaign=scene.campaign,
            scene=scene,
            author_type=AuthorType.GM,
            content=content,
            visibility=Visibility.PUBLIC,
        )

    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def send_private_message(request, scene_id, player_id):
    """Master posts a PRIVATE message to a specific player.

    This triggers a turn for that single player only.
    """
    scene = get_object_or_404(Scene.objects.select_related("campaign"), pk=scene_id)
    player = get_object_or_404(Player, pk=player_id, campaign=scene.campaign)
    content = (request.POST.get("content") or "").strip()
    run_turn_flag = request.POST.get("run_turn", "1") == "1"

    if not content:
        return HttpResponseBadRequest("empty content")

    if run_turn_flag:
        turn_engine.start_turn(
            scene=scene,
            gm_message_text=content,
            selected_players=[player],
            private_to_player=player,
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

    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


@require_http_methods(["POST"])
def set_mode(request, scene_id):
    scene = get_object_or_404(Scene, pk=scene_id)
    mode = request.POST.get("mode")
    if mode in dict(TurnMode.choices):
        scene.mode = mode
        scene.save(update_fields=["mode", "updated_at"])
    return redirect(reverse("scene", kwargs={"scene_id": scene.pk}))


def player_messages(request, player_id, visibility):
    """HTMX partial: returns messages for a player with given visibility."""
    player = get_object_or_404(Player.objects.select_related("campaign"), pk=player_id)
    scene_id = request.GET.get("scene")
    scene = None
    if scene_id:
        scene = get_object_or_404(Scene, pk=scene_id, campaign=player.campaign)
    qs = Message.objects.filter(scene=scene, author_player=player)
    if visibility == "private":
        qs = qs.filter(visibility=Visibility.PRIVATE_GM_PLAYER)
    elif visibility == "public":
        qs = qs.filter(visibility=Visibility.PUBLIC)
    msgs = list(qs.order_by("created_at"))
    return render(request, "rpg/_player_messages.html", {
        "player": player, "messages": msgs, "scene": scene,
    })