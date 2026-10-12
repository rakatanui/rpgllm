"""Owner-scoped campaign creation and editing through the GM workbench."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from rpg.models import Campaign
from rpg.tests.factories import make_campaign, make_scene


def make_staff(username):
    return get_user_model().objects.create_user(
        username=username,
        password="test-password-12345",
        is_active=True,
        is_staff=True,
    )


def authorized_client(user, **kwargs):
    client = Client(**kwargs)
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client


def campaign_data(name="A new campaign", **kwargs):
    return {
        "name": name,
        "description": "Description for players and GM",
        "system_prompt": "World rules, not player secrets",
        "shared_memory": "Established facts",
        **kwargs,
    }


@pytest.mark.django_db
def test_staff_creates_campaign_owned_by_themselves_and_ignores_owner_in_post():
    alice = make_staff("campaign-alice")
    bob = make_staff("campaign-bob")
    client = authorized_client(alice)
    create = reverse("campaign_create")

    page = client.get(create)
    assert page.status_code == 200
    assert "Create campaign" in page.content.decode()
    assert 'name="csrfmiddlewaretoken"' in page.content.decode()
    assert 'name="owner"' not in page.content.decode()

    saved = client.post(create, campaign_data(owner=str(bob.pk)))
    assert saved.status_code == 302
    assert saved["Location"] == reverse("campaigns")
    campaign = Campaign.objects.get(name="A new campaign")
    assert campaign.owner_id == alice.pk
    assert campaign.description == "Description for players and GM"
    assert campaign.system_prompt == "World rules, not player secrets"
    assert campaign.shared_memory == "Established facts"

    index = client.get(reverse("campaigns"))
    assert index.status_code == 200
    html = index.content.decode()
    assert "A new campaign" in html
    assert reverse("campaign_edit", args=[campaign.pk]) in html


@pytest.mark.django_db
def test_campaigns_page_exposes_existing_scenes_and_edit_link():
    user = make_staff("campaign-index")
    campaign = make_campaign("A campaign with scenes", owner=user)
    scene = make_scene(campaign, name="Prologue")
    response = authorized_client(user).get(reverse("campaigns"))
    text = response.content.decode()
    assert response.status_code == 200
    assert "A campaign with scenes" in text
    assert "Prologue" in text
    assert reverse("scene", args=[scene.pk]) in text
    assert reverse("campaign_edit", args=[campaign.pk]) in text
    assert reverse("campaign_create") in text


@pytest.mark.django_db
def test_staff_edits_own_campaign_without_changing_owner():
    alice = make_staff("campaign-edit-alice")
    bob = make_staff("campaign-edit-bob")
    campaign = make_campaign("Original", owner=alice, shared_memory="Old memory")
    client = authorized_client(alice)
    url = reverse("campaign_edit", args=[campaign.pk])

    page = client.get(url)
    assert page.status_code == 200
    assert "Old memory" in page.content.decode()

    result = client.post(
        url, campaign_data("Renamed", owner=str(bob.pk), shared_memory="New memory")
    )
    assert result.status_code == 302
    campaign.refresh_from_db()
    assert campaign.name == "Renamed"
    assert campaign.owner_id == alice.pk
    assert campaign.shared_memory == "New memory"


@pytest.mark.django_db
def test_other_staff_cannot_read_or_edit_foreign_or_unassigned_campaigns():
    alice = make_staff("campaign-private-alice")
    bob = make_staff("campaign-private-bob")
    foreign = make_campaign("Foreign secret", owner=bob)
    legacy = Campaign.objects.create(name="Legacy unassigned")
    client = authorized_client(alice)

    index = client.get(reverse("campaigns"))
    assert "Foreign secret" not in index.content.decode()
    assert "Legacy unassigned" not in index.content.decode()
    for c in (foreign, legacy):
        url = reverse("campaign_edit", args=[c.pk])
        assert client.get(url).status_code == 404
        assert client.post(url, campaign_data("HACKED")).status_code == 404
        c.refresh_from_db()
        assert c.name != "HACKED"


@pytest.mark.django_db
def test_superuser_can_edit_unassigned_and_other_users_campaigns():
    owner = make_staff("campaign-owner")
    root = get_user_model().objects.create_superuser(
        "campaign-root", "root@example.test", "strong-admin-pass"
    )
    other = make_campaign("Owned", owner=owner)
    orphan = Campaign.objects.create(name="Orphan")
    client = authorized_client(root)

    index = client.get(reverse("campaigns"))
    assert other in index.context["campaigns"]
    assert orphan in index.context["campaigns"]
    assert "Unassigned" in index.content.decode()

    for campaign in (other, orphan):
        url = reverse("campaign_edit", args=[campaign.pk])
        assert client.get(url).status_code == 200
        response = client.post(url, campaign_data("Edited by admin"))
        assert response.status_code == 302
        campaign.refresh_from_db()
        assert campaign.name == "Edited by admin"
    assert other.owner_id == owner.pk
    assert orphan.owner_id is None

    created = client.post(reverse("campaign_create"), campaign_data("Admin campaign"))
    assert created.status_code == 302
    assert Campaign.objects.get(name="Admin campaign").owner_id == root.pk


@pytest.mark.django_db
def test_invalid_campaign_forms_preserve_values_without_saving():
    alice = make_staff("campaign-invalid")
    client = authorized_client(alice)
    create = reverse("campaign_create")

    empty = client.post(create, {})
    assert empty.status_code == 200
    assert "name" in empty.context["form"].errors
    assert not Campaign.objects.filter(owner=alice).exists()

    invalid = client.post(create, campaign_data("    "))
    assert invalid.status_code == 200
    assert invalid.context["form"].errors
    assert not Campaign.objects.filter(owner=alice).exists()

    invalid_long = client.post(create, campaign_data("Z" * 201))
    assert invalid_long.status_code == 200
    assert "name" in invalid_long.context["form"].errors
    assert not Campaign.objects.filter(owner=alice).exists()

    existing = make_campaign("Existing", owner=alice)
    update = client.post(
        reverse("campaign_edit", args=[existing.pk]),
        campaign_data("   ", shared_memory="Should not save"),
    )
    assert update.status_code == 200
    existing.refresh_from_db()
    assert existing.name == "Existing"
    assert existing.shared_memory != "Should not save"


@pytest.mark.django_db
def test_user_supplied_campaign_html_is_escaped():
    user = make_staff("campaign-xss")
    campaign = make_campaign("<script>alert(1)</script>", owner=user)
    campaign.description = "<img src=x onerror=alert(1)>"
    campaign.save(update_fields=["description"])
    client = authorized_client(user)

    index = client.get(reverse("campaigns")).content.decode()
    editor = client.get(reverse("campaign_edit", args=[campaign.pk])).content.decode()
    assert "&lt;script&gt;" in index
    assert "<script>alert(1)</script>" not in index
    assert "&lt;img" in index
    assert "<img src=x onerror=alert(1)>" not in index
    assert "&lt;script&gt;" in editor


@pytest.mark.django_db
def test_creation_and_update_reject_non_staff_and_anonymous():
    create = reverse("campaign_create")
    editor = reverse("campaign_edit", args=[99999])
    anonymous = Client()
    for url in (create, editor):
        assert anonymous.get(url).status_code == 302
        assert anonymous.post(url, campaign_data()).status_code == 302

    ordinary = get_user_model().objects.create_user(
        "campaign-ordinary", password="test-password"
    )
    client = authorized_client(ordinary)
    for url in (create, editor):
        assert client.get(url).status_code == 403
        assert client.post(url, campaign_data()).status_code == 403
    assert not Campaign.objects.exists()


@pytest.mark.django_db
def test_campaign_write_requires_csrf_even_when_staff_is_logged_in():
    alice = make_staff("campaign-csrf")
    client = authorized_client(alice, enforce_csrf_checks=True)
    create = reverse("campaign_create")
    assert client.post(create, campaign_data()).status_code == 403
    assert not Campaign.objects.filter(owner=alice).exists()

    assert client.get(create).status_code == 200
    token = client.cookies["csrftoken"].value
    assert client.post(
        create, campaign_data(), HTTP_X_CSRFTOKEN=token
    ).status_code == 302

    campaign = Campaign.objects.get(owner=alice)
    editor = reverse("campaign_edit", args=[campaign.pk])
    assert Client(enforce_csrf_checks=True).post(
        editor, campaign_data("No login")
    ).status_code == 302
    assert client.post(
        editor, campaign_data("No csrf"), HTTP_X_CSRFTOKEN="bad-token"
    ).status_code == 403
    campaign.refresh_from_db()
    assert campaign.name == "A new campaign"


@pytest.mark.django_db
@override_settings(
    PUBLIC_PLAYER_HOST="players.example.test",
    ALLOWED_HOSTS=["testserver", "mraz.local", "players.example.test"],
)
def test_public_player_host_cannot_reach_campaign_editor():
    gm = make_staff("campaign-public")
    campaign = make_campaign("Private", owner=gm)
    client = authorized_client(gm, HTTP_HOST="players.example.test")

    assert client.get(reverse("campaign_create")).status_code == 404
    assert client.post(reverse("campaign_create"), campaign_data()).status_code == 404
    assert client.get(reverse("campaign_edit", args=[campaign.pk])).status_code == 404


@pytest.mark.django_db
def test_campaign_editor_allows_only_get_and_post():
    user = make_staff("campaign-methods")
    campaign = make_campaign("Stable", owner=user)
    client = authorized_client(user)
    assert client.put(reverse("campaign_edit", args=[campaign.pk]), campaign_data()).status_code == 405
    assert client.delete(reverse("campaign_edit", args=[campaign.pk])).status_code == 405
    assert client.put(reverse("campaign_create"), campaign_data()).status_code == 405
    campaign.refresh_from_db()
    assert campaign.name == "Stable"
