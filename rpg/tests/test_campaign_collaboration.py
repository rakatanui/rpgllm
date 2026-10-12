"""Campaign co-GMs share gameplay, never owner-only administration."""

from datetime import date

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from rpg.models import (
    Campaign, CampaignCollaborator, CampaignPlan, ModelConfig,
    Player, Scene, TurnMode,
)
from rpg.tests.factories import make_campaign, make_player, make_scene


def gm(username, *, is_active=True, is_staff=True, superuser=False):
    if superuser:
        return get_user_model().objects.create_superuser(
            username, f"{username}@example.test", "strong-admin-secret",
        )
    return get_user_model().objects.create_user(
        username=username, password="strong-secret-password",
        is_active=is_active, is_staff=is_staff,
    )


def client_for(user, **kwargs):
    client = Client(**kwargs)
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client


def url(campaign, name, *other):
    return reverse(name, args=[campaign.pk, *other])


def add(client, campaign, username):
    return client.post(url(campaign, "campaign_team_add"), {
        "username": username,
    })


@pytest.mark.django_db
def test_owner_can_add_existing_staff_co_master_and_share_campaign():
    owner = gm("team-owner")
    co = gm("team-co")
    outsider = gm("team-outsider")
    campaign = make_campaign("Shared world", owner=owner)
    player = make_player(campaign, "Character")
    scene = make_scene(campaign, "Scene one", participants=[player])
    client = client_for(owner)

    page = client.get(url(campaign, "campaign_team"))
    assert page.status_code == 200
    assert owner.username in page.content.decode()
    assert 'name="csrfmiddlewaretoken"' in page.content.decode()
    assert page.context["can_manage"] is True

    added = add(client, campaign, co.username)
    assert added.status_code == 302
    assert CampaignCollaborator.objects.filter(
        campaign=campaign, user=co
    ).count() == 1
    assert campaign.owner == owner

    other = client_for(co)
    index = other.get(reverse("campaigns"))
    assert campaign in index.context["campaigns"]
    assert "Shared with you" in index.content.decode()
    assert url(campaign, "campaign_detail") in index.content.decode()

    for page_url in (
        url(campaign, "campaign_detail"),
        url(campaign, "campaign_edit"),
        url(campaign, "campaign_player_edit", player.pk),
        url(campaign, "campaign_scene_edit", scene.pk),
        url(campaign, "campaign_timeline"),
        reverse("scene", args=[scene.pk]),
    ):
        assert other.get(page_url).status_code == 200
    assert campaign not in client_for(outsider).get(reverse("campaigns")).context["campaigns"]


@pytest.mark.django_db
def test_co_master_can_edit_campaign_players_scenes_and_private_calendar():
    owner, co = gm("team-work-owner"), gm("team-work-co")
    campaign = make_campaign("Campaign", owner=owner)
    assert add(client_for(owner), campaign, co.username).status_code == 302
    client = client_for(co)
    changed = client.post(url(campaign, "campaign_edit"), {
        "name": "Shared campaign", "description": "Edited by co-master",
        "system_prompt": "The world rules", "shared_memory": "Some facts",
        "owner": str(co.pk),
    })
    assert changed.status_code == 302
    campaign.refresh_from_db()
    assert campaign.name == "Shared campaign"
    assert campaign.owner == owner

    player = client.post(url(campaign, "campaign_player_create"), {
        "display_name": "Explorer", "transport": "HUMAN",
        "character_prompt": "Background", "character_summary": "Hello",
        "characteristics": "", "abilities": "", "memory_summary": "",
        "model_config": "", "fallback_model_config": "",
        "manual_chat_label": "", "manual_chat_url": "",
        "manual_chat_context_mode": "FULL",
    })
    assert player.status_code == 302
    assert campaign.players.filter(display_name="Explorer").exists()

    char = campaign.players.get(display_name="Explorer")
    new_scene = client.post(url(campaign, "campaign_scene_create"), {
        "name": "Opening scene", "description": "A mysterious street",
        "dialogue_language": "Russian", "mode": TurnMode.MANUAL,
        "memory_summary": "", "previous_scenes": [],
        "participants": [str(char.pk)],
    })
    assert new_scene.status_code == 302
    assert campaign.scenes.filter(name="Opening scene").exists()

    update_time = client.post(url(campaign, "campaign_clock_update"), {
        "world_date": "2022-10-14", "world_time": "13:15",
    })
    assert update_time.status_code == 302
    campaign.refresh_from_db()
    assert campaign.world_date == date(2022, 10, 14)
    assert campaign.world_time.hour == 13


@pytest.mark.django_db
def test_co_master_cannot_grant_or_revoke_access_or_touch_lifecycle():
    owner, co, newcomer = (
        gm("team-limits-owner"), gm("team-limits-co"), gm("team-limits-new")
    )
    campaign = make_campaign("Protected campaign", owner=owner)
    assert add(client_for(owner), campaign, co.username).status_code == 302
    co_client = client_for(co)
    assert co_client.get(url(campaign, "campaign_team")).status_code == 200
    assert co_client.get(url(campaign, "campaign_team")).context["can_manage"] is False
    assert "Add co-GM" not in co_client.get(url(campaign, "campaign_team")).content.decode()

    assert add(co_client, campaign, newcomer.username).status_code == 404
    assert co_client.post(
        url(campaign, "campaign_team_remove", co.pk),
    ).status_code == 404
    assert not CampaignCollaborator.objects.filter(
        campaign=campaign, user=newcomer,
    ).exists()

    for action in ("archive", "unarchive", "delete", "recover", "purge"):
        lifecycle = url(campaign, "campaign_lifecycle", action)
        assert co_client.get(lifecycle).status_code in (403, 404)
        assert co_client.post(lifecycle, {
            "confirm_name": campaign.name,
        }).status_code in (403, 404)
    html = co_client.get(reverse("campaigns")).content.decode()
    assert url(campaign, "campaign_lifecycle", "archive") not in html
    assert url(campaign, "campaign_lifecycle", "delete") not in html
    campaign.refresh_from_db()
    assert campaign.owner == owner and campaign.archived_at is None


@pytest.mark.django_db
def test_access_revocation_is_immediate_even_with_existing_authenticated_session():
    owner, co = gm("team-revoke-owner"), gm("team-revoke-co")
    campaign = make_campaign("Revocable", owner=owner)
    scene = make_scene(campaign, "GM notes")
    assert add(client_for(owner), campaign, co.username).status_code == 302
    other = client_for(co)
    assert other.get(url(campaign, "campaign_detail")).status_code == 200
    assert other.get(reverse("scene", args=[scene.pk])).status_code == 200

    removed = client_for(owner).post(url(campaign, "campaign_team_remove", co.pk))
    assert removed.status_code == 302
    assert not CampaignCollaborator.objects.filter(campaign=campaign, user=co).exists()
    for page_url in (
        url(campaign, "campaign_detail"),
        url(campaign, "campaign_edit"),
        url(campaign, "campaign_team"),
        url(campaign, "campaign_timeline"),
        reverse("scene", args=[scene.pk]),
        url(campaign, "campaign_scene_create"),
    ):
        assert other.get(page_url).status_code == 404
    assert campaign not in other.get(reverse("campaigns")).context["campaigns"]
    assert other.get(reverse("campaigns")).status_code == 200


@pytest.mark.django_db
def test_reject_duplicates_owner_inactive_and_player_accounts():
    owner = gm("team-validation-owner")
    co = gm("team-validation-co")
    ordinary = gm("team-validation-player", is_staff=False)
    inactive = gm("team-validation-inactive", is_active=False)
    campaign = make_campaign(owner=owner)
    client = client_for(owner)

    for username in (
        owner.username, ordinary.username, inactive.username, "not-a-user",
    ):
        r = add(client, campaign, username)
        assert r.status_code == 400
        assert r.context["form"].errors
    assert not campaign.collaborators.exists()

    assert add(client, campaign, co.username).status_code == 302
    again = add(client, campaign, co.username)
    assert again.status_code == 400
    assert campaign.collaborators.count() == 1
    assert add(client, campaign, "").status_code == 400


@pytest.mark.django_db
def test_cross_campaign_isolation_and_independent_memberships():
    owner1, owner2, co, outsider = (
        gm("team-boundary-owner1"), gm("team-boundary-owner2"),
        gm("team-boundary-co"), gm("team-boundary-outsider"),
    )
    a = make_campaign("Alpha", owner=owner1)
    b = make_campaign("Bravo", owner=owner2)
    p = make_player(b, "Secret")
    s = make_scene(b, "Secret scene", participants=[p])
    assert add(client_for(owner1), a, co.username).status_code == 302
    other = client_for(co)
    for url_ in (
        url(b, "campaign_detail"),
        url(b, "campaign_team"),
        url(b, "campaign_player_edit", p.pk),
        url(b, "campaign_scene_edit", s.pk),
        reverse("scene", args=[s.pk]),
    ):
        assert other.get(url_).status_code == 404
    assert other.get(url(a, "campaign_detail")).status_code == 200
    assert add(client_for(outsider), a, owner2.username).status_code == 404

    assert add(client_for(owner2), b, co.username).status_code == 302
    assert other.get(url(b, "campaign_detail")).status_code == 200
    assert client_for(owner1).post(
        url(a, "campaign_team_remove", co.pk)
    ).status_code == 302
    assert other.get(url(a, "campaign_detail")).status_code == 404
    assert other.get(url(b, "campaign_detail")).status_code == 200


@pytest.mark.django_db
def test_member_list_remains_accessible_in_archives_but_not_mutable():
    owner, co = gm("team-archive-owner"), gm("team-archive-co")
    campaign = make_campaign(owner=owner)
    assert add(client_for(owner), campaign, co.username).status_code == 302
    archive = client_for(owner).post(
        url(campaign, "campaign_lifecycle", "archive"), {
            "confirm_name": campaign.name,
        }
    )
    assert archive.status_code == 302
    assert client_for(co).get(url(campaign, "campaign_detail")).status_code == 200
    assert client_for(co).get(url(campaign, "campaign_team")).status_code == 200
    assert client_for(owner).post(
        url(campaign, "campaign_team_remove", co.pk)
    ).status_code == 409
    assert client_for(owner).post(
        url(campaign, "campaign_lifecycle", "unarchive"),
    ).status_code == 302
    assert client_for(owner).post(
        url(campaign, "campaign_team_remove", co.pk)
    ).status_code == 302


@pytest.mark.django_db
def test_owner_can_recycle_campaign_and_co_master_cannot_see_its_bin():
    owner, co = gm("team-trash-owner"), gm("team-trash-co")
    campaign = make_campaign(owner=owner)
    assert add(client_for(owner), campaign, co.username).status_code == 302
    owner_client = client_for(owner)
    assert owner_client.post(url(campaign, "campaign_lifecycle", "delete"), {
        "confirm_name": campaign.name,
    }).status_code == 302
    outsider = client_for(co)
    assert outsider.get(url(campaign, "campaign_detail")).status_code == 404
    assert campaign not in outsider.get(reverse("campaigns")).context["deleted_campaigns"]
    assert campaign in owner_client.get(reverse("campaigns")).context["deleted_campaigns"]
    assert owner_client.post(
        url(campaign, "campaign_lifecycle", "recover"),
    ).status_code == 302
    assert outsider.get(url(campaign, "campaign_detail")).status_code == 200


@pytest.mark.django_db
def test_admin_scopes_records_to_owner_or_member_and_disallows_owner_takeover():
    owner, co, unrelated = (
        gm("team-admin-owner"), gm("team-admin-co"), gm("team-admin-other")
    )
    campaign = make_campaign("Shared admin", owner=owner)
    other = make_campaign("Invisible admin", owner=unrelated)
    player = make_player(campaign, "Accessible")
    other_player = make_player(other, "Not accessible")
    assert add(client_for(owner), campaign, co.username).status_code == 302

    request = RequestFactory().get("/admin/rpg/campaign/")
    request.user = co
    ca = admin.site._registry[Campaign]
    pa = admin.site._registry[Player]
    assert campaign in ca.get_queryset(request)
    assert other not in ca.get_queryset(request)
    assert player in pa.get_queryset(request)
    assert other_player not in pa.get_queryset(request)
    assert ca.has_view_permission(request, campaign)
    assert ca.has_change_permission(request, campaign)
    assert "owner" in ca.get_readonly_fields(request, campaign)
    assert not ca.has_change_permission(request, other)

    campaign.owner = co  # mimic a forged admin form that tried to assign owner
    ca.save_model(request, campaign, form=None, change=True)
    campaign.refresh_from_db()
    assert campaign.owner_id == owner.pk


@pytest.mark.django_db
def test_membership_automatically_disables_if_user_loses_staff_or_active_status():
    owner, co = gm("team-disabled-owner"), gm("team-disabled-co")
    campaign = make_campaign(owner=owner)
    assert add(client_for(owner), campaign, co.username).status_code == 302
    original_client = client_for(co)
    assert original_client.get(url(campaign, "campaign_detail")).status_code == 200

    co.is_staff = False
    co.save(update_fields=["is_staff"])
    assert original_client.get(url(campaign, "campaign_detail")).status_code == 403

    co.is_staff = True
    co.is_active = False
    co.save(update_fields=["is_staff", "is_active"])
    assert original_client.get(url(campaign, "campaign_detail")).status_code in (302, 403)
    assert CampaignCollaborator.objects.filter(campaign=campaign, user=co).exists()


@pytest.mark.django_db
def test_only_valid_staff_can_access_team_and_csrf_blocks_forged_actions():
    owner, co = gm("team-csrf-owner"), gm("team-csrf-co")
    campaign = make_campaign(owner=owner)
    form_url = url(campaign, "campaign_team_add")
    assert Client().post(form_url, {"username": co.username}).status_code == 302
    person = gm("team-csrf-ordinary", is_staff=False)
    assert client_for(person).post(form_url, {"username": co.username}).status_code == 403
    client = client_for(owner, enforce_csrf_checks=True)
    assert client.post(form_url, {"username": co.username}).status_code == 403
    assert client.get(url(campaign, "campaign_team")).status_code == 200
    token = client.cookies["csrftoken"].value
    assert client.post(form_url, {
        "username": co.username,
    }, HTTP_X_CSRFTOKEN=token).status_code == 302
    assert client.post(
        url(campaign, "campaign_team_remove", co.pk)
    ).status_code == 403
    assert client.post(
        url(campaign, "campaign_team_remove", co.pk),
        HTTP_X_CSRFTOKEN=token,
    ).status_code == 302
    assert client.get(form_url).status_code == 405
    assert client.get(url(campaign, "campaign_team_remove", co.pk)).status_code == 405


@pytest.mark.django_db
@override_settings(
    PUBLIC_PLAYER_HOST="play.example.test",
    ALLOWED_HOSTS=["testserver", "play.example.test"],
)
def test_public_player_host_exposes_no_team_roster_or_membership_actions():
    owner, co = gm("team-public-owner"), gm("team-public-co")
    campaign = make_campaign(owner=owner)
    client = client_for(owner, HTTP_HOST="play.example.test")
    assert client.get(url(campaign, "campaign_team")).status_code == 404
    assert client.post(
        url(campaign, "campaign_team_add"), {"username": co.username},
    ).status_code == 404
    assert client.post(
        url(campaign, "campaign_team_remove", co.pk),
    ).status_code == 404
    assert not campaign.collaborators.exists()


@pytest.mark.django_db
def test_superuser_may_add_co_master_to_unassigned_campaign():
    root, co = gm("team-superuser", superuser=True), gm("team-root-add")
    unassigned = Campaign.objects.create(name="Legacy", owner=None)
    assert client_for(root).get(url(unassigned, "campaign_team")).status_code == 200
    assert add(client_for(root), unassigned, co.username).status_code == 302
    assert client_for(co).get(url(unassigned, "campaign_detail")).status_code == 200
    assert client_for(co).get(url(unassigned, "campaign_lifecycle", "archive")).status_code == 404
    assert unassigned.owner_id is None
