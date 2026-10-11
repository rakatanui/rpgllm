"""Authentication and authorization boundaries for the Game Master workbench."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from rpg.models import PlayerTransport, TurnMode
from rpg.tests.factories import make_campaign, make_player, make_scene


def make_user(username, *, staff=False, active=True):
    return get_user_model().objects.create_user(
        username=username,
        password="a-long-test-password-12345",
        is_staff=staff,
        is_active=active,
    )


@pytest.mark.django_db
def test_anonymous_gm_pages_and_post_actions_redirect_without_changing_data():
    campaign = make_campaign()
    player = make_player(campaign, "AI")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    client = Client()

    urls = [
        reverse("campaigns"),
        reverse("scene", kwargs={"scene_id": scene.pk}),
        reverse("search_history", kwargs={"scene_id": scene.pk}),
        reverse("scene_fragment", kwargs={"scene_id": scene.pk}),
        reverse("private_channel", kwargs={"scene_id": scene.pk, "player_id": player.pk}),
        reverse("execution_debug", kwargs={"scene_id": scene.pk, "execution_id": 1}),
        reverse("start_model_gm", kwargs={"scene_id": scene.pk}),
        reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
        reverse("regenerate_human_access", kwargs={"scene_id": scene.pk, "player_id": player.pk}),
    ]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 302, url
        assert response["Location"].startswith(reverse("gm_login") + "?next=")

    response = client.post(
        reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
        {"content": "Unauthorized action", "run_turn": "0"},
    )
    assert response.status_code == 302
    assert not scene.messages.filter(content="Unauthorized action").exists()


@pytest.mark.django_db
def test_anonymous_htmx_polling_asks_for_full_page_redirect():
    response = Client(HTTP_HX_REQUEST="true").get(
        reverse("scene_fragment", kwargs={"scene_id": 123})
    )
    assert response.status_code == 403
    assert response["HX-Redirect"].startswith(reverse("gm_login") + "?next=")


@pytest.mark.django_db
def test_authenticated_non_staff_cannot_read_or_modify_gm_workbench():
    campaign = make_campaign()
    scene = make_scene(campaign)
    client = Client()
    client.force_login(make_user("player-login"), backend="django.contrib.auth.backends.ModelBackend")

    assert client.get(reverse("campaigns")).status_code == 403
    assert client.get(reverse("scene", kwargs={"scene_id": scene.pk})).status_code == 403
    assert client.post(
        reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
        {"content": "Non-staff injection", "run_turn": "0"},
    ).status_code == 403
    assert not scene.messages.exists()


@pytest.mark.django_db
def test_staff_login_logout_and_gm_access():
    make_user("gm", staff=True)
    client = Client()
    login = reverse("gm_login")

    assert client.get(login).status_code == 200
    response = client.post(
        login,
        {"username": "gm", "password": "a-long-test-password-12345"},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("campaigns")
    assert client.get(reverse("campaigns")).status_code == 200

    assert client.get(reverse("gm_logout")).status_code == 405
    response = client.post(reverse("gm_logout"))
    assert response.status_code == 302
    assert client.get(reverse("campaigns")).status_code == 302


@pytest.mark.django_db
def test_non_staff_and_inactive_credentials_do_not_authenticate():
    make_user("ordinary")
    make_user("disabled-gm", staff=True, active=False)
    for username in ("ordinary", "disabled-gm"):
        client = Client()
        response = client.post(
            reverse("gm_login"),
            {"username": username, "password": "a-long-test-password-12345"},
        )
        assert response.status_code == 200
        assert not client.session.get("_auth_user_id")
        assert client.get(reverse("campaigns")).status_code == 302


@pytest.mark.django_db
def test_login_next_target_cannot_redirect_to_external_host():
    make_user("gm-safe", staff=True)
    response = Client().post(
        reverse("gm_login") + "?next=https://evil.example/phish",
        {"username": "gm-safe", "password": "a-long-test-password-12345"},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("campaigns")


@pytest.mark.django_db
def test_player_bearer_url_remains_public_without_gm_credentials():
    campaign = make_campaign()
    human = make_player(campaign, "Human", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    token = scene.scene_participants.get(player=human).human_access_token
    client = Client()
    response = client.get(reverse("human_player_client", kwargs={"access_token": token}))
    assert response.status_code == 200
    assert client.get(reverse("campaigns")).status_code == 302


@pytest.mark.django_db
@override_settings(
    PUBLIC_PLAYER_HOST="players.example.test",
    ALLOWED_HOSTS=["testserver", "players.example.test", "mraz.local"],
)
def test_public_player_hostname_never_exposes_gm_even_to_staff():
    campaign = make_campaign()
    scene = make_scene(campaign)
    client = Client(HTTP_HOST="players.example.test")
    client.force_login(make_user("gm-public", staff=True), backend="django.contrib.auth.backends.ModelBackend")
    assert client.get(reverse("scene", kwargs={"scene_id": scene.pk})).status_code == 404
    assert client.get(reverse("gm_login")).status_code == 404


@pytest.mark.django_db
def test_staff_can_read_master_scene_and_send_message():
    gm = make_user("actual-gm", staff=True)
    campaign = make_campaign(owner=gm)
    scene = make_scene(campaign, mode=TurnMode.MANUAL)
    client = Client()
    client.force_login(gm, backend="django.contrib.auth.backends.ModelBackend")

    assert client.get(reverse("scene", kwargs={"scene_id": scene.pk})).status_code == 200
    result = client.post(
        reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
        {"content": "Authorized action", "run_turn": "0"},
    )
    assert result.status_code == 302
    assert scene.messages.filter(content="Authorized action").exists()


@pytest.mark.django_db
def test_login_and_logout_enforce_csrf_on_writes():
    make_user("csrf-gm", staff=True)
    client = Client(enforce_csrf_checks=True)
    login = reverse("gm_login")
    logout = reverse("gm_logout")
    credentials = {
        "username": "csrf-gm",
        "password": "a-long-test-password-12345",
    }

    assert client.post(login, credentials).status_code == 403
    assert client.get(login).status_code == 200
    csrf_token = client.cookies["csrftoken"].value

    signed_in = client.post(login, credentials, HTTP_X_CSRFTOKEN=csrf_token)
    assert signed_in.status_code == 302
    assert client.get(reverse("campaigns")).status_code == 200

    assert client.post(logout).status_code == 403
    assert client.get(reverse("campaigns")).status_code == 200

    signed_out = client.post(logout, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
    assert signed_out.status_code == 302
    assert client.get(reverse("campaigns")).status_code == 302
