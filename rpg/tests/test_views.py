"""View-level tests for scene message relationships."""
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from rpg.models import AuthorType, Message, TurnMode, Visibility
from rpg.services import turn_engine
from rpg.services.llm import LLMResponse, MockLLMClient
from rpg.tests.factories import make_campaign, make_player, make_scene


class PrivateNoteClient(MockLLMClient):
    def generate(self, *, system_prompt, messages, model, temperature=0.7):
        return LLMResponse(
            raw_text="{}",
            action_type="ACT",
            public="Мила проверяет дверную ручку.",
            private_to_gm="Мила прислушивается к звукам за дверью.",
        )


@pytest.mark.django_db
def test_scene_links_private_note_to_public_action(mock_backend):
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    with patch(
        "rpg.services.turn_engine.get_llm_client",
        return_value=PrivateNoteClient(),
    ):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="Перед тобой закрытая дверь.",
            selected_players=[mila],
        )

    public_message = Message.objects.get(
        turn=result.turn,
        author_type=AuthorType.PLAYER,
        visibility=Visibility.PUBLIC,
    )
    private_message = Message.objects.get(
        turn=result.turn,
        author_type=AuthorType.PLAYER,
        visibility=Visibility.PRIVATE_GM_PLAYER,
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    entries = response.context["player_private"][mila.pk]
    entry = next(item for item in entries if item["message"].pk == private_message.pk)
    assert entry["public_message"].pk == public_message.pk
    assert entry["trigger_message"].content == "Перед тобой закрытая дверь."

    html = response.content.decode()
    assert "К публичному ходу:" in html
    assert f'href="#message-{public_message.pk}"' in html
    assert f'id="message-{public_message.pk}"' in html



@pytest.mark.django_db
def test_switch_to_round_requires_confirmed_order():
    campaign = make_campaign()
    make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    response = Client().post(
        reverse("set_mode", kwargs={"scene_id": scene.pk}),
        {"mode": TurnMode.ROUND},
    )

    assert response.status_code == 400
    scene.refresh_from_db()
    assert scene.mode == TurnMode.MANUAL
    assert scene.round_order == []


@pytest.mark.django_db
def test_switch_to_round_saves_confirmed_order_and_resets_active():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, active_player_index=0)

    response = Client().post(
        reverse("set_mode", kwargs={"scene_id": scene.pk}),
        {
            "mode": TurnMode.ROUND,
            "round_order": [str(lucien.pk), str(mila.pk)],
        },
    )

    assert response.status_code == 302
    scene.refresh_from_db()
    assert scene.mode == TurnMode.ROUND
    assert scene.round_order == [lucien.pk, mila.pk]
    assert scene.active_player_index == 0


@pytest.mark.django_db
def test_switch_to_round_rejects_duplicate_or_missing_players():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    response = Client().post(
        reverse("set_mode", kwargs={"scene_id": scene.pk}),
        {
            "mode": TurnMode.ROUND,
            "round_order": [str(mila.pk), str(mila.pk)],
        },
    )

    assert response.status_code == 400
    scene.refresh_from_db()
    assert scene.mode == TurnMode.MANUAL


@pytest.mark.django_db
def test_round_public_send_is_blocked_without_confirmed_order(mock_backend):
    campaign = make_campaign()
    make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.ROUND, round_order=[])

    response = Client().post(
        reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
        {"content": "Что вы делаете?", "run_turn": "1"},
    )

    assert response.status_code == 400
    assert not scene.turns.exists()


@pytest.mark.django_db
def test_scene_renders_large_gm_textarea_and_round_setup():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert 'class="gm-composer-input"' in html
    assert '<textarea' in html
    assert "Round order" in html
    assert "Confirm order" in html
    assert "Люсьен" in html
    assert "Мила" in html



@pytest.mark.django_db
def test_private_gm_message_is_informational_by_default(mock_backend):
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    with patch("rpg.views.turn_engine.start_turn") as start_turn:
        response = Client().post(
            reverse(
                "send_private_message",
                kwargs={"scene_id": scene.pk, "player_id": mila.pk},
            ),
            {"content": "Ты узнаёшь символ на двери."},
        )

    assert response.status_code == 302
    start_turn.assert_not_called()
    message = Message.objects.get(
        scene=scene,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        author_type=AuthorType.GM,
        private_player=mila,
    )
    assert message.content == "Ты узнаёшь символ на двери."


@pytest.mark.django_db
def test_private_gm_message_calls_model_only_when_explicitly_asked(mock_backend):
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    with patch("rpg.views.turn_engine.start_turn") as start_turn:
        response = Client().post(
            reverse(
                "send_private_message",
                kwargs={"scene_id": scene.pk, "player_id": mila.pk},
            ),
            {
                "content": "Что ты делаешь с этой информацией?",
                "run_turn": "1",
            },
        )

    assert response.status_code == 302
    start_turn.assert_called_once()
    assert start_turn.call_args.kwargs["private_to_player"] == mila


@pytest.mark.django_db
def test_unread_private_marker_clears_when_gm_opens_channel():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)
    private = Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=mila,
        content="Секрет для мастера.",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=mila,
        gm_unread=True,
    )

    page = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    assert page.status_code == 200
    assert f'data-unread-player="{mila.pk}"' in page.content.decode()

    response = Client().post(
        reverse(
            "mark_private_read",
            kwargs={"scene_id": scene.pk, "player_id": mila.pk},
        )
    )

    assert response.status_code == 204
    private.refresh_from_db()
    assert private.gm_unread is False


@pytest.mark.django_db
def test_scene_uses_fixed_viewport_layout_and_private_ask_is_opt_in():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert 'class="scene-page"' in html
    assert 'class="scene-shell"' in html
    assert 'class="gm-composer-sticky"' in html
    assert "Ask for response" in html
    assert 'name="run_turn" value="1"> Ask for response' in html
