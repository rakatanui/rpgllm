"""Public bearer client authorization, abuse prevention and upload regressions."""

import io
import logging
import uuid
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse
from PIL import Image, PngImagePlugin

from rpg.models import (
    AuthorType, CharacterAppearance, Message, PlayerRateBucket,
    PlayerTransport, TurnMode, Visibility, SceneParticipant,
)
from rpg.player_security import PlayerTokenRedactionFilter
from rpg.tests.factories import make_campaign, make_player, make_scene


@pytest.fixture
def player_urls(db):
    campaign = make_campaign()
    player = make_player(campaign, "Human", transport=PlayerTransport.HUMAN)
    second = make_player(campaign, "Other", transport=PlayerTransport.HUMAN)
    scene = make_scene(
        campaign, mode=TurnMode.MANUAL, participants=[player, second],
    )
    token = scene.scene_participants.get(player=player).human_access_token
    other_token = scene.scene_participants.get(player=second).human_access_token

    def url(name, token=token, **kwargs):
        return reverse(name, kwargs={"access_token": token, **kwargs})

    return scene, player, second, other_token, url


@pytest.mark.django_db
def test_bearer_pages_never_cache_or_send_referrers(player_urls):
    scene, player, _, _, url = player_urls
    client = Client()
    response = client.get(url("human_player_client"))
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert "noindex" in response["X-Robots-Tag"]

    for name in ("human_player_status", "human_player_fragment", "human_player_qr"):
        other = client.get(url(name))
        assert other.status_code == 200
        assert other["Cache-Control"] == "private, no-store"
        assert other["Referrer-Policy"] == "no-referrer"

    missing = client.get(reverse("human_player_client", kwargs={"access_token": uuid.uuid4()}))
    assert missing.status_code == 404
    assert missing["Cache-Control"] == "private, no-store"


@pytest.mark.django_db
def test_bearer_post_rejects_foreign_browser_origin_without_side_effects(player_urls):
    scene, player, _, _, url = player_urls
    endpoint = url("human_send_ooc")
    client = Client(enforce_csrf_checks=True)
    cross = client.post(endpoint, {"content": "INJECTED"}, HTTP_ORIGIN="https://attacker.test")
    assert cross.status_code == 403
    assert not Message.objects.filter(content__contains="INJECTED").exists()

    fetch = client.post(endpoint, {"content": "INJECTED"}, HTTP_SEC_FETCH_SITE="cross-site")
    assert fetch.status_code == 403
    assert not Message.objects.filter(content__contains="INJECTED").exists()

    valid = client.post(endpoint, {"content": "AUTHORIZED"}, HTTP_ORIGIN="http://testserver")
    assert valid.status_code == 302
    assert Message.objects.filter(content__contains="AUTHORIZED", author_player=player).exists()
    assert valid["Referrer-Policy"] == "no-referrer"


@pytest.mark.django_db
def test_player_request_limits_apply_to_token_across_client_instances(player_urls):
    scene, player, other, other_token, url = player_urls
    with override_settings(PLAYER_RATE_LIMITS={
        "ooc": (2, 60), "qr": (2, 60),
        "turn": (2, 60), "appearance": (2, 60), "upload": (2, 300),
    }):
        endpoint = url("human_send_ooc")
        assert Client().post(endpoint, {"content": "first"}).status_code == 302
        assert Client().post(endpoint, {"content": "second"}).status_code == 302
        blocked = Client().post(endpoint, {"content": "third"})
        assert blocked.status_code == 429
        assert int(blocked["Retry-After"]) >= 1
        assert "third" not in "\n".join(scene.messages.values_list("content", flat=True))

        # Same IP, different bearer token. An abusive player must not block
        # someone else who shares a connection or an Internet gateway.
        independent = Client().post(
            url("human_send_ooc", token=other_token), {"content": "other player"}
        )
        assert independent.status_code == 302
        assert Message.objects.filter(author_player=other, content__contains="other player").exists()

        # Refresh polling is not a write or an expensive QR regeneration.
        for _ in range(8):
            assert Client().get(url("human_player_status")).status_code == 200

        assert Client().get(url("human_player_qr")).status_code == 200
        assert Client().get(url("human_player_qr")).status_code == 200
        qr_block = Client().get(url("human_player_qr"))
        assert qr_block.status_code == 429


@pytest.mark.django_db
def test_rate_bucket_keys_do_not_store_raw_token(player_urls):
    _, _, _, _, url = player_urls
    raw_token = str(url("human_send_ooc").split("/")[2])
    assert Client().post(url("human_send_ooc"), {"content": "message"}).status_code == 302
    keys = list(PlayerRateBucket.objects.values_list("key", flat=True))
    assert keys and all(raw_token not in key and len(key) == 64 for key in keys)


@pytest.mark.django_db
def test_revoked_or_replaced_token_cannot_reach_any_bearer_endpoint(player_urls):
    scene, player, _, _, url = player_urls
    token = scene.scene_participants.get(player=player)
    old_url = url("human_player_client")
    token.human_access_enabled = False
    token.save(update_fields=["human_access_enabled"])
    for name in ("human_player_client", "human_player_status", "human_player_qr"):
        assert Client().get(url(name)).status_code == 404
    assert Client().post(url("human_send_ooc"), {"content": "denied"}).status_code == 404

    token.human_access_enabled = True
    token.human_access_token = uuid.uuid4()
    token.save(update_fields=["human_access_enabled", "human_access_token"])
    assert Client().get(old_url).status_code == 404
    assert Client().get(url("human_player_client", token=token.human_access_token)).status_code == 200


@pytest.mark.django_db
def test_cross_player_object_ids_never_grant_access(player_urls):
    scene, player, other, other_token, url = player_urls
    other_appearance = CharacterAppearance.objects.create(
        player=other, name="Other appearance", is_primary=True
    )
    client = Client()
    assert client.get(
        url("human_appearance_image", appearance_id=other_appearance.pk, image_kind="portrait")
    ).status_code == 404
    assert client.post(
        url("set_human_current_appearance", appearance_id=other_appearance.pk)
    ).status_code == 404
    assert client.get(
        url("human_episode_detail", episode_id=scene.pk + 1000)
    ).status_code == 404


@pytest.mark.django_db
def test_player_upload_reencodes_image_and_strips_embedded_metadata(player_urls, tmp_path):
    _, player, _, _, url = player_urls
    image = Image.new("RGB", (10, 10), color="blue")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-key", "SECRET_EXIF_NOTE_TO_STRIP")
    source = io.BytesIO()
    image.save(source, format="PNG", pnginfo=metadata)

    with override_settings(MEDIA_ROOT=tmp_path):
        response = Client().post(
            url("upload_human_character_image"),
            {"image": SimpleUploadedFile("portrait.png", source.getvalue(), content_type="image/png")},
        )
        assert response.status_code == 302
        player.refresh_from_db()
        assert player.character_image
        player.character_image.open("rb")
        content = player.character_image.read()
        assert b"SECRET_EXIF_NOTE_TO_STRIP" not in content
        with Image.open(io.BytesIO(content)) as sanitized:
            assert sanitized.format == "PNG"
            assert sanitized.size == (10, 10)
        image_response = Client().get(url("human_character_image"))
        assert image_response.status_code == 200
        assert image_response["Cache-Control"] == "private, no-store"
        assert image_response["X-Content-Type-Options"] == "nosniff"


@pytest.mark.django_db
def test_player_upload_rejects_excessive_decoded_pixels(player_urls, tmp_path):
    _, player, _, _, url = player_urls
    image = Image.new("RGB", (3, 3), color="red")
    contents = io.BytesIO()
    image.save(contents, "PNG")

    with patch("rpg.forms.MAX_CHARACTER_IMAGE_PIXELS", 4), override_settings(MEDIA_ROOT=tmp_path):
        response = Client().post(
            url("upload_human_character_image"),
            {"image": SimpleUploadedFile("oversize.png", contents.getvalue(), content_type="image/png")},
        )
    assert response.status_code == 400
    player.refresh_from_db()
    assert not player.character_image


@pytest.mark.django_db
def test_closed_scene_cannot_change_character_image(player_urls, tmp_path):
    scene, player, _, _, url = player_urls
    scene.is_closed = True
    scene.save(update_fields=["is_closed"])
    image = Image.new("RGB", (2, 2), color="green")
    contents = io.BytesIO()
    image.save(contents, "PNG")
    with override_settings(MEDIA_ROOT=tmp_path):
        assert Client().post(
            url("upload_human_character_image"),
            {"image": SimpleUploadedFile("picture.png", contents.getvalue(), content_type="image/png")},
        ).status_code == 400
    assert Client().post(url("remove_human_character_image")).status_code == 400


@pytest.mark.django_db
def test_player_ooc_post_has_bounded_body(player_urls):
    _, player, _, _, url = player_urls
    result = Client().post(url("human_send_ooc"), {"content": "X" * 8001})
    assert result.status_code == 400
    assert not Message.objects.filter(author_player=player).exists()


@pytest.mark.django_db
def test_player_text_escaped_in_html(player_urls):
    scene, player, _, _, url = player_urls
    Message.objects.create(
        campaign=scene.campaign, scene=scene, author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC,
        content='<img src=x onerror="alert(1)">',
    )
    html = Client().get(url("human_player_client")).content.decode()
    assert "&lt;img" in html
    assert "<img src=x onerror=" not in html


def test_player_token_logging_filter_redacts_url_uuid():
    token = uuid.uuid4()
    record = logging.LogRecord(
        "django.request", logging.WARNING, "test.py", 1,
        f"Not Found: /play/{token}/status/", (), None,
    )
    assert PlayerTokenRedactionFilter().filter(record) is True
    assert str(token) not in record.getMessage()
    assert "/play/[redacted]/status/" in record.getMessage()


@pytest.mark.django_db
def test_player_submission_rejects_oversized_public_and_private_body(player_urls):
    _, _, _, _, url = player_urls
    response = Client().post(
        url("submit_human_response", execution_id=999999),
        {"action_type": "ACT", "content": "A" * 9000, "private_to_gm": "B" * 8000},
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_player_history_rejects_huge_search_and_numeric_cursor(player_urls):
    _, _, _, _, url = player_urls
    assert Client().get(
        url("human_episode_search"), {"q": "X" * 201}
    ).status_code == 400
    assert Client().get(
        url("human_feed_page", channel="public"), {"before": "9" * 10000}
    ).status_code == 400


@pytest.mark.django_db
def test_expired_player_rate_buckets_can_be_cleaned_without_touching_live_limits(player_urls):
    from datetime import timedelta
    from django.core.management import call_command
    from django.utils import timezone

    _, _, _, _, url = player_urls
    assert Client().post(url("human_send_ooc"), {"content": "hi"}).status_code == 302
    live = PlayerRateBucket.objects.first()
    assert live is not None
    PlayerRateBucket.objects.create(
        key="a" * 64,
        expires_at=timezone.now() - timedelta(hours=1),
        count=99,
    )
    call_command("cleanup_player_rate_buckets")
    assert PlayerRateBucket.objects.filter(key="a" * 64).exists() is False
    assert PlayerRateBucket.objects.filter(key=live.key).exists()
