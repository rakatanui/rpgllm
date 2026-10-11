"""Test factories / helpers shared across test modules."""
from django.contrib.auth import get_user_model
from rpg.models import Campaign, ModelConfig, Player, Scene, SceneParticipant, TurnMode


def make_campaign(name="Test Campaign", **kw):
    defaults = dict(
        system_prompt="Test system prompt.",
        description="",
    )
    defaults.update(kw)
    if "owner" not in defaults and "owner_id" not in defaults:
        # Existing GM view tests predate ownership. Bind those campaigns to
        # their shared authenticated fixture user, never to a real superuser.
        gm, _ = get_user_model().objects.get_or_create(
            username="__rpg_test_gm__",
            defaults={"is_staff": True, "is_active": True},
        )
        defaults["owner"] = gm
    return Campaign.objects.create(name=name, **defaults)


def make_model(name="Mock", gateway_model="mock-echo", **kw):
    return ModelConfig.objects.create(name=name, gateway_model=gateway_model, **kw)


def make_player(campaign, display_name="P", character_prompt="", model_config=None, **kw):
    return Player.objects.create(
        campaign=campaign,
        display_name=display_name,
        character_prompt=character_prompt or f"I am {display_name}.",
        model_config=model_config,
        **kw,
    )


def make_scene(
    campaign,
    name="Scene 1",
    mode=TurnMode.ROUND,
    round_order=None,
    participants=None,
    predecessors=None,
    **kw,
):
    s = Scene.objects.create(
        campaign=campaign,
        name=name,
        mode=mode,
        round_order=round_order or [],
        **kw,
    )
    if participants is None:
        participants = list(
            Player.objects.filter(campaign=campaign).order_by("created_at", "pk")
        )
    SceneParticipant.objects.bulk_create(
        [
            SceneParticipant(scene=s, player=player, order=index)
            for index, player in enumerate(participants)
        ]
    )
    if predecessors:
        s.previous_scenes.add(*predecessors)
    return s


def make_three_players(campaign, model_config=None):
    lucien = make_player(campaign, "Lucien", model_config=model_config)
    mila = make_player(campaign, "Mila", model_config=model_config)
    mathis = make_player(campaign, "Mathis", model_config=model_config)
    return lucien, mila, mathis