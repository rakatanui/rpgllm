"""Campaign dashboard, characters, scenes, and safe participant-roster edits."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from rpg.models import (
    Campaign, ModelConfig, Player, PlayerTransport, Scene, SceneParticipant,
    Turn, TurnMode, TurnState,
)
from rpg.tests.factories import make_campaign, make_player, make_scene


def staff(name):
    return get_user_model().objects.create_user(
        username=name, password="workbench-test-password", is_staff=True
    )


def logged_in(user, **options):
    client = Client(**options)
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client


def character_data(name="Character A", **kw):
    return {
        "display_name": name, "transport": PlayerTransport.HUMAN,
        "character_prompt": "Remember your background.", "character_summary": "A pilot",
        "characteristics": "Agile", "abilities": "Flight", "memory_summary": "Secret",
        "model_config": "", "fallback_model_config": "",
        "manual_chat_label": "", "manual_chat_url": "",
        "manual_chat_context_mode": "FULL", **kw,
    }


def scene_data(name="Dock", *, players=(), **kw):
    return {
        "name": name, "description": "The old harbor",
        "dialogue_language": "Polish", "mode": TurnMode.MANUAL,
        "memory_summary": "Previously established public facts",
        "participants": [str(p.pk) for p in players], **kw,
    }


@pytest.mark.django_db
def test_campaign_dashboard_lists_scoped_players_and_scenes():
    owner = staff("wb-overview")
    camp = make_campaign("World", owner=owner)
    player = make_player(camp, "Pilot", transport=PlayerTransport.HUMAN)
    scene = make_scene(camp, "Harbor", participants=[player])
    client = logged_in(owner)

    response = client.get(reverse("campaign_detail", args=[camp.pk]))
    text = response.content.decode()
    assert response.status_code == 200
    assert "World" in text and "Pilot" in text and "Harbor" in text
    assert reverse("campaign_player_edit", args=[camp.pk, player.pk]) in text
    assert reverse("campaign_scene_edit", args=[camp.pk, scene.pk]) in text
    assert reverse("campaign_player_create", args=[camp.pk]) in text
    assert reverse("campaign_scene_create", args=[camp.pk]) in text
    assert reverse("campaign_detail", args=[camp.pk]) in client.get(reverse("campaigns")).content.decode()
    assert reverse("campaign_detail", args=[camp.pk]) in client.get(reverse("scene", args=[scene.pk])).content.decode()


@pytest.mark.django_db
def test_create_and_edit_character_in_own_campaign():
    user = staff("wb-create-character")
    camp = make_campaign(owner=user)
    model = ModelConfig.objects.create(name="Model for workbench", gateway_model="mock-echo")
    client = logged_in(user)

    create = reverse("campaign_player_create", args=[camp.pk])
    assert client.get(create).status_code == 200
    saved = client.post(
        create, character_data("New human", model_config=str(model.pk))
    )
    assert saved.status_code == 302
    assert saved["Location"] == reverse("campaign_detail", args=[camp.pk])

    player = camp.players.get(display_name="New human")
    assert player.transport == PlayerTransport.HUMAN
    assert player.model_config_id == model.pk
    assert player.abilities == "Flight"

    edit = reverse("campaign_player_edit", args=[camp.pk, player.pk])
    assert client.get(edit).status_code == 200
    changed = client.post(edit, character_data("Renamed", abilities="Magic"))
    assert changed.status_code == 302
    player.refresh_from_db()
    assert player.campaign_id == camp.pk
    assert player.display_name == "Renamed"
    assert player.abilities == "Magic"
    assert player.model_config is None


@pytest.mark.django_db
def test_character_name_unique_per_campaign_and_invalid_fields_leave_data_untouched():
    user = staff("wb-unique")
    camp = make_campaign(owner=user)
    old = make_player(camp, "Existing")
    client = logged_in(user)

    invalid = client.post(
        reverse("campaign_player_create", args=[camp.pk]), character_data("Existing")
    )
    assert invalid.status_code == 200
    assert invalid.context["form"].errors
    assert camp.players.count() == 1

    invalid = client.post(reverse("campaign_player_edit", args=[camp.pk, old.pk]), {})
    assert invalid.status_code == 200
    old.refresh_from_db()
    assert old.display_name == "Existing"

    second = make_campaign("Second", owner=user)
    assert client.post(
        reverse("campaign_player_create", args=[second.pk]), character_data("Existing")
    ).status_code == 302
    assert second.players.count() == 1


@pytest.mark.django_db
def test_external_chat_link_change_invalidates_old_chat_memory():
    user = staff("wb-chat")
    camp = make_campaign(owner=user)
    player = make_player(
        camp, "Bot", transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_url="https://chatgpt.com/c/old",
        manual_chat_initialized=True,
    )
    client = logged_in(user)
    url = reverse("campaign_player_edit", args=[camp.pk, player.pk])
    assert client.post(url, character_data(
        "Bot", transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_url="https://chatgpt.com/c/new"
    )).status_code == 302
    player.refresh_from_db()
    assert player.manual_chat_initialized is False


@pytest.mark.django_db
def test_create_scene_with_selected_participants_and_configuration():
    user = staff("wb-scene-create")
    camp = make_campaign(owner=user)
    p1 = make_player(camp, "One")
    p2 = make_player(camp, "Two")
    client = logged_in(user)

    create = reverse("campaign_scene_create", args=[camp.pk])
    page = client.get(create)
    assert page.status_code == 200
    assert "One" in page.content.decode() and "Two" in page.content.decode()
    result = client.post(create, scene_data("Night port", players=[p1, p2]))
    assert result.status_code == 302
    scene = camp.scenes.get(name="Night port")
    assert scene.description == "The old harbor"
    assert scene.dialogue_language == "Polish"
    assert scene.mode == TurnMode.MANUAL
    assert scene.memory_summary == "Previously established public facts"
    assert list(scene.scene_participants.order_by("order").values_list("player_id", flat=True)) == [
        p1.pk, p2.pk,
    ]
    assert scene.round_order == []
    assert scene.active_player_index == 0


@pytest.mark.django_db
def test_scene_roster_change_preserves_remaining_token_and_reconfirms_round_order():
    user = staff("wb-roster")
    camp = make_campaign(owner=user)
    p1 = make_player(camp, "One")
    p2 = make_player(camp, "Two")
    p3 = make_player(camp, "Three")
    scene = make_scene(
        camp, "Waiting", participants=[p1, p2], mode=TurnMode.ROUND,
        round_order=[p1.pk, p2.pk], active_player_index=1
    )
    keep = scene.scene_participants.get(player=p1)
    old_token = keep.human_access_token
    keep.human_access_enabled = False
    keep.save(update_fields=["human_access_enabled"])

    client = logged_in(user)
    result = client.post(
        reverse("campaign_scene_edit", args=[camp.pk, scene.pk]),
        scene_data("Updated", players=[p1, p3], mode=TurnMode.ROUND),
    )
    assert result.status_code == 302
    scene.refresh_from_db()
    assert scene.name == "Updated"
    assert scene.round_order == [] and scene.active_player_index == 0
    assert set(scene.scene_participants.values_list("player_id", flat=True)) == {p1.pk, p3.pk}
    retained = scene.scene_participants.get(player=p1)
    assert retained.pk == keep.pk
    assert retained.human_access_token == old_token
    assert retained.human_access_enabled is False
    assert not scene.scene_participants.filter(player=p2).exists()


@pytest.mark.django_db
def test_scene_without_roster_changes_preserves_confirmed_round_order():
    user = staff("wb-round-preserve")
    camp = make_campaign(owner=user)
    p1 = make_player(camp, "One")
    p2 = make_player(camp, "Two")
    scene = make_scene(
        camp, "Waiting", participants=[p1, p2], mode=TurnMode.ROUND,
        round_order=[p1.pk, p2.pk], active_player_index=1,
    )
    response = logged_in(user).post(
        reverse("campaign_scene_edit", args=[camp.pk, scene.pk]),
        scene_data("Only metadata", players=[p1, p2], mode=TurnMode.ROUND),
    )
    assert response.status_code == 302
    scene.refresh_from_db()
    assert scene.round_order == [p1.pk, p2.pk]
    assert scene.active_player_index == 1


@pytest.mark.django_db
def test_scene_rejects_another_campaigns_player_even_when_same_gm_owns_both():
    user = staff("wb-foreign-roster")
    good = make_campaign("Good", owner=user)
    bad = make_campaign("Other", owner=user)
    foreign = make_player(bad, "Foreign")
    client = logged_in(user)

    response = client.post(
        reverse("campaign_scene_create", args=[good.pk]),
        scene_data("Invalid", players=[foreign]),
    )
    assert response.status_code == 200
    assert "participants" in response.context["form"].errors
    assert not good.scenes.filter(name="Invalid").exists()


@pytest.mark.django_db
def test_scene_roster_locked_after_a_turn_but_scene_metadata_can_change():
    user = staff("wb-roster-history")
    camp = make_campaign(owner=user)
    p1 = make_player(camp, "One")
    p2 = make_player(camp, "Two")
    scene = make_scene(camp, "Running", mode=TurnMode.MANUAL, participants=[p1])
    keep = scene.scene_participants.get(player=p1)
    Turn.objects.create(scene=scene, mode=TurnMode.MANUAL, state=TurnState.COMPLETED)
    client = logged_in(user)
    url = reverse("campaign_scene_edit", args=[camp.pk, scene.pk])

    reject = client.post(url, scene_data("Should not save", players=[p1, p2]))
    assert reject.status_code == 200
    assert "participants" in reject.context["form"].errors
    scene.refresh_from_db()
    assert scene.name == "Running"
    assert list(scene.scene_participants.values_list("player_id", flat=True)) == [p1.pk]

    allowed = client.post(url, scene_data("New label", players=[p1]))
    assert allowed.status_code == 302
    scene.refresh_from_db()
    assert scene.name == "New label"
    assert scene.scene_participants.get(player=p1).human_access_token == keep.human_access_token


@pytest.mark.django_db
def test_closed_scene_cannot_be_modified_in_editor():
    user = staff("wb-closed")
    camp = make_campaign(owner=user)
    p = make_player(camp, "One")
    scene = make_scene(camp, "Finished", is_closed=True, participants=[p])
    url = reverse("campaign_scene_edit", args=[camp.pk, scene.pk])
    client = logged_in(user)
    assert client.get(url).status_code == 403
    assert client.post(url, scene_data("Bad", players=[p])).status_code == 403
    assert reverse("campaign_scene_edit", args=[camp.pk, scene.pk]) not in client.get(
        reverse("campaign_detail", args=[camp.pk])
    ).content.decode()
    scene.refresh_from_db()
    assert scene.name == "Finished"


@pytest.mark.django_db
def test_foreign_campaign_or_cross_campaign_object_ids_are_not_accessible():
    alice, bob = staff("wb-alice"), staff("wb-bob")
    own = make_campaign("Own", owner=alice)
    foreign = make_campaign("Foreign secret", owner=bob)
    foreign_player = make_player(foreign, "Their secret")
    foreign_scene = make_scene(foreign, "Secret harbor", participants=[foreign_player])
    client = logged_in(alice)

    for url in (
        reverse("campaign_detail", args=[foreign.pk]),
        reverse("campaign_player_create", args=[foreign.pk]),
        reverse("campaign_player_edit", args=[foreign.pk, foreign_player.pk]),
        reverse("campaign_scene_create", args=[foreign.pk]),
        reverse("campaign_scene_edit", args=[foreign.pk, foreign_scene.pk]),
        reverse("campaign_player_edit", args=[own.pk, foreign_player.pk]),
        reverse("campaign_scene_edit", args=[own.pk, foreign_scene.pk]),
    ):
        assert client.get(url).status_code == 404
        assert client.post(url, {}).status_code == 404
    assert "Foreign secret" not in client.get(reverse("campaigns")).content.decode()


@pytest.mark.django_db
def test_unassigned_campaign_only_superuser_can_manage():
    user = staff("wb-staff")
    root = get_user_model().objects.create_superuser(
        username="wb-root", email="admin@example.test", password="strong-admin-pass"
    )
    orphan = Campaign.objects.create(name="Orphan")
    assert logged_in(user).get(reverse("campaign_detail", args=[orphan.pk])).status_code == 404
    client = logged_in(root)
    assert client.get(reverse("campaign_detail", args=[orphan.pk])).status_code == 200
    created = client.post(
        reverse("campaign_player_create", args=[orphan.pk]), character_data("Caretaker")
    )
    assert created.status_code == 302
    assert orphan.players.filter(display_name="Caretaker").exists()


@pytest.mark.django_db
def test_anonymous_nonstaff_csrf_and_http_method_protection():
    user = staff("wb-access")
    camp = make_campaign(owner=user)
    url = reverse("campaign_scene_create", args=[camp.pk])
    client = Client()
    assert client.get(url).status_code == 302
    assert client.post(url, scene_data()).status_code == 302
    regular = get_user_model().objects.create_user("wb-regular", password="some-password")
    assert logged_in(regular).post(url, scene_data()).status_code == 403

    csrf = logged_in(user, enforce_csrf_checks=True)
    assert csrf.post(url, scene_data()).status_code == 403
    assert csrf.get(url).status_code == 200
    token = csrf.cookies["csrftoken"].value
    assert csrf.post(
        url, scene_data(), HTTP_X_CSRFTOKEN=token
    ).status_code == 302
    assert logged_in(user).put(url, scene_data()).status_code == 405
    assert camp.scenes.count() == 1


@pytest.mark.django_db
@override_settings(
    PUBLIC_PLAYER_HOST="players.example.test",
    ALLOWED_HOSTS=["testserver", "mraz.local", "players.example.test"],
)
def test_player_only_host_does_not_expose_campaign_workbench():
    user = staff("wb-host")
    camp = make_campaign(owner=user)
    client = logged_in(user, HTTP_HOST="players.example.test")
    assert client.get(reverse("campaign_detail", args=[camp.pk])).status_code == 404
    assert client.get(reverse("campaign_player_create", args=[camp.pk])).status_code == 404
    assert client.get(reverse("campaign_scene_create", args=[camp.pk])).status_code == 404


@pytest.mark.django_db
def test_untrusted_display_values_render_as_escaped_html():
    user = staff("wb-xss")
    camp = make_campaign("<script>alert(1)</script>", owner=user)
    make_player(camp, '<img src=x onerror=alert(1)>')
    html = logged_in(user).get(reverse("campaign_detail", args=[camp.pk])).content.decode()
    assert "&lt;script&gt;" in html
    assert "&lt;img" in html
    assert "<script>alert(1)</script>" not in html
    assert '<img src=x onerror=alert(1)>' not in html
