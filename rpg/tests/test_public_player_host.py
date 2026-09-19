"""Regression tests for the Internet-facing HUMAN player hostname boundary."""
import pytest
from django.test import Client, override_settings
from django.urls import reverse

from rpg.models import PlayerTransport, TurnMode
from rpg.tests.factories import make_campaign, make_player, make_scene


PUBLIC_HOST_SETTINGS = {
    "PUBLIC_PLAYER_HOST": "players.example.test",
    "ALLOWED_HOSTS": ["testserver", "mraz.local", "players.example.test"],
    "CSRF_TRUSTED_ORIGINS": [
        "http://mraz.local",
        "https://players.example.test",
    ],
}


@pytest.mark.django_db
@override_settings(**PUBLIC_HOST_SETTINGS)
def test_public_player_host_allows_only_token_client_not_gm_workbench():
    campaign = make_campaign()
    human = make_player(
        campaign,
        "Живой",
        transport=PlayerTransport.HUMAN,
    )
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[human],
    )
    token = scene.scene_participants.get(player=human).human_access_token
    client = Client(HTTP_HOST="players.example.test")

    player_response = client.get(
        reverse("human_player_client", kwargs={"access_token": token})
    )
    gm_response = client.get(reverse("scene", kwargs={"scene_id": scene.pk}))
    admin_response = client.get("/admin/")
    root_response = client.get("/")

    assert player_response.status_code == 200
    assert gm_response.status_code == 404
    assert admin_response.status_code == 404
    assert root_response.status_code == 404


@pytest.mark.django_db
@override_settings(**PUBLIC_HOST_SETTINGS)
def test_local_gm_hostname_remains_available_when_public_host_is_enabled():
    campaign = make_campaign()
    player = make_player(campaign, "Игрок")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[player],
    )

    response = Client(HTTP_HOST="mraz.local").get(
        reverse("scene", kwargs={"scene_id": scene.pk})
    )

    assert response.status_code == 200



@pytest.mark.django_db
@override_settings(**PUBLIC_HOST_SETTINGS)
def test_gm_scene_copies_public_https_player_link_when_configured():
    campaign = make_campaign()
    human = make_player(
        campaign,
        "Живой",
        transport=PlayerTransport.HUMAN,
    )
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[human],
    )
    token = scene.scene_participants.get(player=human).human_access_token

    response = Client(HTTP_HOST="mraz.local").get(
        reverse("scene", kwargs={"scene_id": scene.pk})
    )
    html = response.content.decode()

    expected = f"https://players.example.test{reverse('human_player_client', kwargs={'access_token': token})}"
    assert response.status_code == 200
    assert expected in html
