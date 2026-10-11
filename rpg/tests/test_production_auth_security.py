"""Security regressions: one Game Master must not reach another master's data."""

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse


@pytest.fixture(autouse=True)
def admin_staticfiles_without_manifest(settings):
    # Tests do not run collectstatic, but Django Admin still renders its CSS.
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }

from rpg.models import Campaign, LoreEntry, ModelConfig, Player, Scene
from rpg.tests.factories import make_campaign, make_player, make_scene


@pytest.fixture
def two_masters(db):
    model = get_user_model()
    alice = model.objects.create_user(username="alice", password="a-password", is_staff=True)
    bob = model.objects.create_user(username="bob", password="a-password", is_staff=True)
    campaign_a = make_campaign("Alice only", owner=alice)
    campaign_b = make_campaign("Bob only", owner=bob)
    scene_a = make_scene(campaign_a, name="Secret A")
    scene_b = make_scene(campaign_b, name="Secret B")
    player_a = make_player(campaign_a, "Hidden Alice character")
    player_b = make_player(campaign_b, "Hidden Bob character")
    return alice, bob, campaign_a, campaign_b, scene_a, scene_b, player_a, player_b


@pytest.mark.django_db
def test_workbench_lists_only_owned_campaigns(two_masters):
    alice, _, campaign_a, campaign_b, *_ = two_masters
    client = Client()
    client.force_login(alice, backend="django.contrib.auth.backends.ModelBackend")

    response = client.get(reverse("campaigns"))
    assert response.status_code == 200
    assert campaign_a in response.context["campaigns"]
    assert campaign_b not in response.context["campaigns"]
    assert "Bob only" not in response.content.decode()


@pytest.mark.django_db
def test_url_guards_reject_foreign_scene_and_player_by_id(two_masters):
    alice, _, _, _, scene_a, scene_b, player_a, player_b = two_masters
    client = Client()
    client.force_login(alice, backend="django.contrib.auth.backends.ModelBackend")
    assert client.get(reverse("scene", kwargs={"scene_id": scene_a.pk})).status_code == 200

    for url in [
        reverse("scene", kwargs={"scene_id": scene_b.pk}),
        reverse("search_history", kwargs={"scene_id": scene_b.pk}),
        reverse("scene_fragment", kwargs={"scene_id": scene_b.pk}),
        reverse("private_channel", kwargs={"scene_id": scene_b.pk, "player_id": player_b.pk}),
        reverse("player_messages", kwargs={"player_id": player_b.pk, "visibility": "PUBLIC"}),
    ]:
        assert client.get(url).status_code == 404, url

    forbidden = client.post(
        reverse("send_gm_message", kwargs={"scene_id": scene_b.pk}),
        {"content": "Cross-campaign update", "run_turn": "0"},
    )
    assert forbidden.status_code == 404
    assert not scene_b.messages.filter(content="Cross-campaign update").exists()

    # Neither can Alice attach one of Bob's character IDs to her scene.
    assert client.get(
        reverse("private_channel", kwargs={"scene_id": scene_a.pk, "player_id": player_b.pk})
    ).status_code == 404


@pytest.mark.django_db
def test_admin_list_and_direct_object_reads_are_scoped(two_masters):
    alice, _, a, b, scene_a, scene_b, *_ = two_masters
    client = Client()
    client.force_login(alice, backend="django.contrib.auth.backends.ModelBackend")

    listing = client.get(reverse("admin:rpg_campaign_changelist"))
    assert listing.status_code == 200
    assert "Alice only" in listing.content.decode()
    assert "Bob only" not in listing.content.decode()

    assert client.get(reverse("admin:rpg_campaign_change", args=[a.pk])).status_code == 200
    assert client.get(reverse("admin:rpg_campaign_change", args=[b.pk])).status_code in (302, 403, 404)
    assert client.get(reverse("admin:rpg_scene_change", args=[scene_b.pk])).status_code in (302, 403, 404)


@pytest.mark.django_db
def test_admin_cannot_reference_foreign_campaign_via_forged_form(two_masters):
    alice, _, camp_a, camp_b, *_ = two_masters
    client = Client()
    client.force_login(alice, backend="django.contrib.auth.backends.ModelBackend")
    url = reverse("admin:rpg_loreentry_add")
    data = {
        "campaign": str(camp_b.pk),
        "title": "Exfiltrated lore",
        "category": "SECRET",
        "content": "Not an authorized write.",
        "scope": "GLOBAL",
        "priority": 100,
        "enabled": "on",
        "scenes": [],
        "players": [],
    }
    result = client.post(url, data)
    assert result.status_code in (200, 302, 403)
    assert not LoreEntry.objects.filter(title="Exfiltrated lore").exists()


@pytest.mark.django_db
def test_owner_is_immutable_for_non_superuser_admin(two_masters):
    alice, bob, campaign, *_ = two_masters
    request = RequestFactory().post("/admin/rpg/campaign/%s/change/" % campaign.pk)
    request.user = alice
    model_admin = admin.site._registry[Campaign]
    assert "owner" in model_admin.get_readonly_fields(request, campaign)

    campaign.owner = bob
    model_admin.save_model(request, campaign, form=None, change=True)
    campaign.refresh_from_db()
    assert campaign.owner == alice


@pytest.mark.django_db
def test_related_fk_options_do_not_include_another_master(two_masters):
    alice, _, camp_a, camp_b, *_ = two_masters
    request = RequestFactory().get("/admin/rpg/loreentry/add/")
    request.user = alice
    model_admin = admin.site._registry[LoreEntry]
    field = model_admin.formfield_for_foreignkey(
        LoreEntry._meta.get_field("campaign"), request
    )
    assert list(field.queryset) == [camp_a]
    assert camp_b not in field.queryset


@pytest.mark.django_db
def test_admin_model_configuration_is_superuser_only(two_masters):
    alice, _, *_ = two_masters
    client = Client()
    client.force_login(alice, backend="django.contrib.auth.backends.ModelBackend")
    assert client.get(reverse("admin:rpg_modelconfig_changelist")).status_code == 403


@pytest.mark.django_db
def test_unassigned_campaign_only_visible_to_superuser(two_masters):
    alice, _, *_ = two_masters
    legacy = Campaign.objects.create(name="Legacy unassigned")
    staff_client = Client()
    staff_client.force_login(alice, backend="django.contrib.auth.backends.ModelBackend")
    assert legacy not in staff_client.get(reverse("campaigns")).context["campaigns"]
    assert staff_client.get(reverse("admin:rpg_campaign_change", args=[legacy.pk])).status_code in (
        302, 403, 404,
    )

    root = get_user_model().objects.create_superuser(
        username="root", email="root@example.test", password="a-password"
    )
    superuser_client = Client()
    superuser_client.force_login(root, backend="django.contrib.auth.backends.ModelBackend")
    assert legacy in superuser_client.get(reverse("campaigns")).context["campaigns"]


@pytest.mark.django_db
@override_settings(AXES_FAILURE_LIMIT=3)
def test_login_is_locked_after_repeated_failures():
    get_user_model().objects.create_user(
        username="lockout-gm", password="a-long-test-password-12345", is_staff=True
    )
    client = Client(REMOTE_ADDR="192.0.2.123")
    login = reverse("gm_login")
    for _ in range(3):
        client.post(login, {"username": "lockout-gm", "password": "wrong"})

    locked = client.post(
        login,
        {"username": "lockout-gm", "password": "a-long-test-password-12345"},
    )
    # Axes uses 429 for lockouts by default.
    assert locked.status_code == 429
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@override_settings(
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SECURE_HSTS_SECONDS=3600,
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
)
def test_https_redirect_and_secure_cookie_policy():
    http_client = Client()
    redirect = http_client.get(reverse("gm_login"))
    assert redirect.status_code == 301
    assert redirect["Location"].startswith("https://")

    https_client = Client(HTTP_X_FORWARDED_PROTO="https")
    response = https_client.get(reverse("gm_login"))
    assert response.status_code == 200
    assert response["Strict-Transport-Security"].startswith("max-age=3600")
    assert bool(https_client.cookies["csrftoken"]["secure"])

    user = get_user_model().objects.create_user(
        username="tls-user", password="a-long-test-password-12345", is_staff=True
    )
    login = https_client.post(
        reverse("gm_login"),
        {"username": user.username, "password": "a-long-test-password-12345"},
    )
    assert login.status_code == 302
    assert bool(https_client.cookies["sessionid"]["secure"])
    assert bool(https_client.cookies["sessionid"]["httponly"])
