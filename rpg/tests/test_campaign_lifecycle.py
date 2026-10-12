"""Archive, restore, recycle and guarded campaign erasure workflows."""

from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from rpg.models import (
    Campaign, CampaignPlan, CharacterAppearance, Message, ModelConfig,
    PlayerTransport, Turn, TurnMode, TurnState, Visibility, AuthorType,
)
from rpg.tests.factories import make_campaign, make_player, make_scene


def staff(name, *, superuser=False):
    if superuser:
        return get_user_model().objects.create_superuser(
            username=name, email=f"{name}@example.test", password="safe-password-123",
        )
    return get_user_model().objects.create_user(
        username=name, password="safe-password-123", is_staff=True,
    )


def login(user, **kwargs):
    client = Client(**kwargs)
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client


def target(campaign, action):
    return reverse(
        "campaign_lifecycle",
        kwargs={"campaign_id": campaign.pk, "action": action},
    )


def make_playable(owner):
    campaign = make_campaign("Private story", owner=owner)
    human = make_player(campaign, "Human", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, "Opening", mode=TurnMode.MANUAL, participants=[human])
    Message.objects.create(
        campaign=campaign, scene=scene, author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC, content="The history survives.",
    )
    CampaignPlan.objects.create(
        campaign=campaign, title="NPC journey",
        scheduled_date=date(2022, 10, 12),
    )
    return campaign, human, scene


@pytest.mark.django_db
def test_archive_freezes_writes_and_public_player_token_but_preserves_data():
    user = staff("archive-owner")
    campaign, player, scene = make_playable(user)
    participation = scene.scene_participants.get(player=player)
    original_token = participation.human_access_token
    client = login(user)

    confirmation = client.get(target(campaign, "archive"))
    assert confirmation.status_code == 200
    assert "Archive campaign" in confirmation.content.decode()
    assert campaign.archived_at is None

    invalid = client.post(target(campaign, "archive"), {"confirm_name": "something else"})
    assert invalid.status_code == 400
    assert campaign.archived_at is None

    done = client.post(target(campaign, "archive"), {"confirm_name": campaign.name})
    assert done.status_code == 302
    campaign.refresh_from_db()
    assert campaign.archived_at is not None
    assert campaign.deleted_at is None

    assert Campaign.objects.filter(pk=campaign.pk).exists()
    assert campaign.scenes.count() == 1
    assert campaign.players.count() == 1
    assert campaign.messages.filter(content="The history survives.").exists()
    assert campaign.timeline_plans.filter(title="NPC journey").exists()
    assert scene.scene_participants.get(player=player).human_access_token == original_token

    # GM retains read-only history and story calendar.
    assert client.get(reverse("campaign_detail", args=[campaign.pk])).status_code == 200
    assert client.get(reverse("scene", args=[scene.pk])).status_code == 200
    assert client.get(reverse("campaign_timeline", args=[campaign.pk])).status_code == 200

    assert client.post(reverse("send_gm_message", args=[scene.pk]), {
        "content": "A forbidden new beat", "run_turn": "0",
    }).status_code == 409
    assert client.post(reverse("campaign_clock_update", args=[campaign.pk]), {
        "world_date": "2022-10-15", "world_time": "13:00",
    }).status_code == 409
    assert client.post(reverse("campaign_scene_create", args=[campaign.pk]), {
        "name": "Forbidden follow-up",
    }).status_code == 409
    assert Client().get(reverse("human_player_client", args=[original_token])).status_code == 404
    assert Client().post(reverse("submit_human_response", args=[original_token]), {
        "content": "I act",
    }).status_code == 404

    index = client.get(reverse("campaigns"))
    assert campaign not in index.context["campaigns"]
    assert campaign in index.context["archived_campaigns"]
    assert "ARCHIVED / READ ONLY" in client.get(
        reverse("campaign_detail", args=[campaign.pk])
    ).content.decode()


@pytest.mark.django_db
def test_unarchive_recovers_all_data_and_the_same_player_url():
    user = staff("unarchive-owner")
    campaign, player, scene = make_playable(user)
    token = scene.scene_participants.get(player=player).human_access_token
    client = login(user)
    client.post(target(campaign, "archive"), {"confirm_name": campaign.name})

    result = client.post(target(campaign, "unarchive"))
    assert result.status_code == 302
    campaign.refresh_from_db()
    assert campaign.archived_at is None
    assert Client().get(reverse("human_player_client", args=[token])).status_code == 200
    assert client.get(reverse("campaign_scene_create", args=[campaign.pk])).status_code == 200
    assert campaign.messages.filter(content="The history survives.").exists()


@pytest.mark.django_db
def test_recycle_bin_hides_campaign_and_recover_retains_previous_archive_state():
    owner = staff("trash-owner")
    campaign, player, scene = make_playable(owner)
    token = scene.scene_participants.get(player=player).human_access_token
    client = login(owner)

    client.post(target(campaign, "archive"), {"confirm_name": campaign.name})
    archived_at = Campaign.objects.get(pk=campaign.pk).archived_at
    removed = client.post(target(campaign, "delete"), {"confirm_name": campaign.name})
    assert removed.status_code == 302
    campaign.refresh_from_db()
    assert campaign.deleted_at is not None
    assert campaign.archived_at == archived_at
    assert client.get(reverse("campaign_detail", args=[campaign.pk])).status_code == 404
    assert client.get(reverse("scene", args=[scene.pk])).status_code == 404
    assert client.get(reverse("campaign_timeline", args=[campaign.pk])).status_code == 404
    assert Client().get(reverse("human_player_client", args=[token])).status_code == 404
    listing = client.get(reverse("campaigns"))
    assert campaign in listing.context["deleted_campaigns"]
    assert campaign not in listing.context["archived_campaigns"]
    assert campaign not in listing.context["campaigns"]

    recovered = client.post(target(campaign, "recover"))
    assert recovered.status_code == 302
    campaign.refresh_from_db()
    assert campaign.deleted_at is None
    assert campaign.archived_at == archived_at
    assert client.get(reverse("campaign_detail", args=[campaign.pk])).status_code == 200
    assert Client().get(reverse("human_player_client", args=[token])).status_code == 404
    client.post(target(campaign, "unarchive"))
    assert Client().get(reverse("human_player_client", args=[token])).status_code == 200


@pytest.mark.django_db
def test_delete_active_then_recover_to_active_with_intact_history():
    owner = staff("recover-active")
    campaign, player, scene = make_playable(owner)
    token = scene.scene_participants.get(player=player).human_access_token
    client = login(owner)
    assert client.post(target(campaign, "delete"), {"confirm_name": campaign.name}).status_code == 302
    assert Client().get(reverse("human_player_client", args=[token])).status_code == 404
    assert client.post(target(campaign, "recover")).status_code == 302
    campaign.refresh_from_db()
    assert campaign.deleted_at is None and campaign.archived_at is None
    assert campaign.messages.count() == 1
    assert campaign.timeline_plans.count() == 1
    assert Client().get(reverse("human_player_client", args=[token])).status_code == 200


@pytest.mark.django_db
def test_archive_and_delete_refuse_pending_or_running_turns_without_changing_state():
    user = staff("busy-campaign")
    campaign, player, scene = make_playable(user)
    turn = Turn.objects.create(scene=scene, mode=TurnMode.MANUAL, state=TurnState.RUNNING)
    client = login(user)
    for action in ("archive", "delete"):
        result = client.post(target(campaign, action), {"confirm_name": campaign.name})
        assert result.status_code == 409
        campaign.refresh_from_db()
        assert campaign.archived_at is None and campaign.deleted_at is None

    turn.state = TurnState.COMPLETED
    turn.save(update_fields=["state"])
    assert client.post(target(campaign, "archive"), {"confirm_name": campaign.name}).status_code == 302


@pytest.mark.django_db
def test_lifecycle_actions_refuse_wrong_states_and_http_methods():
    owner = staff("state-guard")
    campaign = make_campaign(owner=owner)
    client = login(owner)
    for action in ("unarchive", "recover", "purge"):
        assert client.get(target(campaign, action)).status_code in (403, 404)
    assert client.get(target(campaign, "invalid-action")).status_code == 404
    assert client.put(target(campaign, "archive")).status_code == 405
    assert client.get(target(campaign, "archive")).status_code == 200
    campaign.refresh_from_db()
    assert campaign.archived_at is None

    assert client.post(target(campaign, "archive"), {"confirm_name": campaign.name}).status_code == 302
    assert client.get(target(campaign, "archive")).status_code == 404
    assert client.post(target(campaign, "archive"), {"confirm_name": campaign.name}).status_code == 404


@pytest.mark.django_db
def test_foreign_nonstaff_anonymous_and_public_player_host_are_denied():
    alice = staff("lifecycle-alice")
    bob = staff("lifecycle-bob")
    campaign, player, scene = make_playable(alice)
    actions = ("archive", "unarchive", "delete", "recover", "purge")
    other = login(bob)
    for action in actions:
        url = target(campaign, action)
        assert other.get(url).status_code in (403, 404)
        assert other.post(url, {"confirm_name": campaign.name}).status_code in (403, 404)
        assert Client().get(url).status_code == 302
        assert Client().post(url, {"confirm_name": campaign.name}).status_code == 302
    regular = get_user_model().objects.create_user(
        "lifecycle-regular", password="a-safe-password",
    )
    assert login(regular).post(
        target(campaign, "archive"), {"confirm_name": campaign.name},
    ).status_code == 403
    with override_settings(
        PUBLIC_PLAYER_HOST="play.example.test",
        ALLOWED_HOSTS=["testserver", "play.example.test"],
    ):
        public = login(alice, HTTP_HOST="play.example.test")
        assert public.get(target(campaign, "archive")).status_code == 404
        assert public.post(
            target(campaign, "archive"), {"confirm_name": campaign.name},
        ).status_code == 404
    campaign.refresh_from_db()
    assert campaign.archived_at is None and campaign.deleted_at is None


@pytest.mark.django_db
def test_csrf_required_for_all_write_actions():
    owner = staff("lifecycle-csrf")
    campaign = make_campaign(owner=owner)
    client = login(owner, enforce_csrf_checks=True)
    archive = target(campaign, "archive")
    assert client.post(archive, {"confirm_name": campaign.name}).status_code == 403
    assert client.get(archive).status_code == 200
    csrf = client.cookies["csrftoken"].value
    assert client.post(
        archive, {"confirm_name": campaign.name}, HTTP_X_CSRFTOKEN=csrf,
    ).status_code == 302
    assert client.post(target(campaign, "unarchive")).status_code == 403
    assert client.post(
        target(campaign, "unarchive"), HTTP_X_CSRFTOKEN=csrf,
    ).status_code == 302


@pytest.mark.django_db
def test_non_superuser_cannot_permanently_purge_even_their_own_campaign():
    owner = staff("no-purge")
    campaign = make_campaign(owner=owner)
    client = login(owner)
    client.post(target(campaign, "delete"), {"confirm_name": campaign.name})
    assert client.get(target(campaign, "purge")).status_code == 403
    assert client.post(
        target(campaign, "purge"), {"confirm_name": campaign.name},
    ).status_code == 403
    assert Campaign.objects.filter(pk=campaign.pk).exists()
    assert "Erase forever" not in client.get(reverse("campaigns")).content.decode()


@pytest.mark.django_db(transaction=True)
def test_superuser_purge_requires_bin_and_confirmation_and_deletes_related_rows(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    root = staff("purge-root", superuser=True)
    owner = staff("purge-user")
    campaign, player, scene = make_playable(owner)
    token = scene.scene_participants.get(player=player).human_access_token
    shared = ModelConfig.objects.create(name="Shared model", gateway_model="mock-echo")
    player.model_config = shared
    player.character_image.save("portrait.png", ContentFile(b"fake-data"), save=True)
    portrait_path = player.character_image.name
    from django.core.files.storage import default_storage
    assert default_storage.exists(portrait_path)
    client = login(root)

    # The purge operation exists only for campaigns already in the bin.
    assert client.get(target(campaign, "purge")).status_code == 404
    assert client.post(target(campaign, "delete"), {
        "confirm_name": campaign.name,
    }).status_code == 302
    assert client.get(target(campaign, "purge")).status_code == 200
    assert client.post(target(campaign, "purge"), {
        "confirm_name": "wrong",
    }).status_code == 400
    assert Campaign.objects.filter(pk=campaign.pk).exists()

    complete = client.post(target(campaign, "purge"), {
        "confirm_name": campaign.name,
    })
    assert complete.status_code == 302
    assert not Campaign.objects.filter(pk=campaign.pk).exists()
    assert not Message.objects.filter(scene_id=scene.pk).exists()
    assert not CampaignPlan.objects.filter(campaign_id=campaign.pk).exists()
    assert ModelConfig.objects.filter(pk=shared.pk).exists()
    assert not default_storage.exists(portrait_path)
    assert Client().get(reverse("human_player_client", args=[token])).status_code == 404


@pytest.mark.django_db
def test_another_campaign_and_global_models_survive_recovery_cycle():
    owner = staff("side-effects")
    first = make_campaign("First", owner=owner)
    second = make_campaign("Second", owner=owner)
    model = ModelConfig.objects.create(name="Global provider", gateway_model="mock-echo")
    client = login(owner)
    client.post(target(first, "delete"), {"confirm_name": first.name})
    first.refresh_from_db()
    assert first.deleted_at is not None
    assert second.deleted_at is None
    assert ModelConfig.objects.filter(pk=model.pk).exists()
    assert second in client.get(reverse("campaigns")).context["campaigns"]


@pytest.mark.django_db
def test_admin_cannot_edit_archived_campaign_or_create_related_objects_for_it():
    owner = staff("admin-frozen")
    campaign, player, scene = make_playable(owner)
    client = login(owner)
    archive = target(campaign, "archive")
    assert client.post(archive, {"confirm_name": campaign.name}).status_code == 302

    from django.contrib import admin
    from rpg.models import Scene as SceneModel
    from django.test import RequestFactory
    request = RequestFactory().get("/admin/rpg/campaign/")
    request.user = owner
    campaign_admin = admin.site._registry[Campaign]
    scene_admin = admin.site._registry[SceneModel]
    campaign.refresh_from_db()
    scene.refresh_from_db()
    assert campaign_admin.has_view_permission(request, campaign)
    assert not campaign_admin.has_change_permission(request, campaign)
    assert not campaign_admin.has_delete_permission(request, campaign)
    assert not scene_admin.has_change_permission(request, scene)
    assert not scene_admin.has_delete_permission(request, scene)

    from django.core.exceptions import PermissionDenied
    with pytest.raises(PermissionDenied):
        scene_admin.delete_model(request, scene)
    campaign.refresh_from_db()
    assert campaign.scenes.count() == 1
