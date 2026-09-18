"""View-level tests for scene message relationships."""
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse

from rpg.models import (
    AuthorType,
    ExecutionState,
    Message,
    Turn,
    TurnExecution,
    TurnMode,
    TurnState,
    Visibility,
)
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
def test_switch_to_round_can_reuse_existing_valid_order():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    response = Client().post(
        reverse("set_mode", kwargs={"scene_id": scene.pk}),
        {"mode": TurnMode.ROUND},
    )

    assert response.status_code == 302
    scene.refresh_from_db()
    assert scene.mode == TurnMode.ROUND
    assert scene.round_order == [lucien.pk, mila.pk]
    assert scene.active_player_index == 0


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
def test_manual_scene_marks_round_as_ready_when_valid_order_is_stored():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert 'data-round-ready="1"' in html


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
def test_manual_public_turn_requires_selected_player(mock_backend):
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    with patch("rpg.views.turn_engine.start_turn") as start_turn:
        response = Client().post(
            reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
            {"content": "Что вы делаете?", "run_turn": "1"},
        )

    assert response.status_code == 400
    start_turn.assert_not_called()
    assert not Message.objects.filter(scene=scene, content="Что вы делаете?").exists()


@pytest.mark.django_db
def test_public_run_turn_checkbox_can_be_unchecked(mock_backend):
    campaign = make_campaign()
    make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    with patch("rpg.views.turn_engine.start_turn") as start_turn:
        response = Client().post(
            reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
            {"content": "Просто запись без вызова модели."},
        )

    assert response.status_code == 302
    start_turn.assert_not_called()
    assert Message.objects.filter(
        scene=scene,
        content="Просто запись без вызова модели.",
        author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC,
    ).exists()


@pytest.mark.django_db
def test_manual_scene_preselects_participants_in_composer():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert (
        f'name="selected_players" value="{mila.pk}" checked'
        in html
    )
    assert (
        f'name="selected_players" value="{lucien.pk}" checked'
        in html
    )


@pytest.mark.django_db
def test_scene_visually_groups_round_and_colors_each_player():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    mathis = make_player(campaign, "Матис")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk, mathis.pk],
        active_player_index=0,
    )

    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.ROUND,
        state=TurnState.COMPLETED,
        participants=[lucien.pk, mila.pk, mathis.pk],
        active_player_id_snapshot=lucien.pk,
    )
    gm = Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        author_type=AuthorType.GM,
        content="Машина остановилась.",
        visibility=Visibility.PUBLIC,
    )
    turn.trigger_message = gm
    turn.save(update_fields=["trigger_message"])

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Люсьен выходит из машины.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        author_type=AuthorType.PLAYER,
        author_player=mila,
        content="Мила остаётся внутри.",
        visibility=Visibility.PUBLIC,
        action_type="ACT_OUT_OF_TURN",
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        author_type=AuthorType.PLAYER,
        author_player=mathis,
        content="[PASS] Матис",
        visibility=Visibility.PUBLIC,
        action_type="PASS",
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert f'data-turn-id="{turn.pk}"' in html
    assert html.count('class="round-block ') == 1
    assert "speaker-player-0" in html
    assert "speaker-player-1" in html
    assert "speaker-player-2" in html
    assert "[ACT]" in html
    assert "[ACT_OUT_OF_TURN]" in html
    assert "[PASS]" in html
    assert "Люсьен выходит из машины." in html
    assert "[PASS] Матис" not in html


@pytest.mark.django_db
def test_latest_failed_execution_shows_single_model_retry_button():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mathis = make_player(campaign, "Матис")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mathis.pk],
        active_player_index=0,
    )

    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.ROUND,
        state=TurnState.FAILED,
        participants=[lucien.pk, mathis.pk],
        active_player_id_snapshot=lucien.pk,
        error="Матис: timed out",
    )
    TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        order_index=0,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
    )
    failed = TurnExecution.objects.create(
        turn=turn,
        player=mathis,
        order_index=1,
        state=ExecutionState.FAILED,
        error="timed out",
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Retry Матис only" in html
    assert "Retry Люсьен only" not in html
    assert reverse(
        "retry_execution",
        kwargs={"scene_id": scene.pk, "execution_id": failed.pk},
    ) in html
    assert "timed out" in html


@pytest.mark.django_db
def test_retry_execution_view_retries_only_requested_failed_execution():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mathis = make_player(campaign, "Матис")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mathis.pk],
        active_player_index=0,
    )
    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.ROUND,
        state=TurnState.FAILED,
        participants=[lucien.pk, mathis.pk],
        active_player_id_snapshot=lucien.pk,
    )
    completed = TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        order_index=0,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
    )
    failed = TurnExecution.objects.create(
        turn=turn,
        player=mathis,
        order_index=1,
        state=ExecutionState.FAILED,
        error="timed out",
    )

    with patch("rpg.views.turn_engine.retry_execution") as retry:
        response = Client().post(
            reverse(
                "retry_execution",
                kwargs={"scene_id": scene.pk, "execution_id": failed.pk},
            )
        )

    assert response.status_code == 302
    retry.assert_called_once()
    assert retry.call_args.args[0].pk == failed.pk
    assert retry.call_args.args[0].pk != completed.pk


@pytest.mark.django_db
def test_retry_execution_view_rejects_completed_execution():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)
    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.MANUAL,
        state=TurnState.COMPLETED,
        participants=[lucien.pk],
    )
    execution = TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        order_index=0,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
    )

    with patch("rpg.views.turn_engine.retry_execution") as retry:
        response = Client().post(
            reverse(
                "retry_execution",
                kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
            )
        )

    assert response.status_code == 400
    retry.assert_not_called()


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
