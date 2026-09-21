"""View-level tests for scene message relationships."""
import base64
import re
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.urls import reverse

from rpg.models import (
    AuthorType,
    CharacterAppearance,
    ExecutionState,
    GameMasterConfig,
    GameMasterExecutionState,
    GameMasterTransport,
    LoreEntry,
    ManualChatContextMode,
    Message,
    MessageRevision,
    PlayerTransport,
    Turn,
    TurnExecution,
    TurnMode,
    TurnState,
    Visibility,
)
from rpg.services import turn_engine
from rpg.services.llm import LLMResponse, MockLLMClient
from rpg.tests.factories import make_campaign, make_model, make_player, make_scene


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
    retry_url = reverse(
        "retry_execution",
        kwargs={"scene_id": scene.pk, "execution_id": failed.pk},
    )
    assert f'action="{retry_url}"' in html
    assert html.count(f'action="{retry_url}"') == 1
    assert "Retry" in html
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
def test_message_output_preserves_multiline_content_and_separates_header():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Люсьен открывает дверь.\n— Оставайтесь здесь.\nОн выходит наружу.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert 'class="msg-header"' in html
    assert 'class="msg-content"' in html
    assert "Люсьен [ACT]" not in html
    assert "Люсьен открывает дверь.\n— Оставайтесь здесь.\nОн выходит наружу." in html


@pytest.mark.django_db
def test_gm_composer_has_dialogue_format_button():
    campaign = make_campaign()
    make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="gm-composer-input"' in html
    assert ">Реплика</button>" in html
    assert "formatSelectedAsDialogue" in html
    assert '"— "' in html


@pytest.mark.django_db
def test_public_and_private_feeds_show_newest_messages_first():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    public_old = Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="PUBLIC OLD",
        visibility=Visibility.PUBLIC,
    )
    public_new = Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="PUBLIC NEW",
        visibility=Visibility.PUBLIC,
    )
    private_old = Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="PRIVATE OLD",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=mila,
    )
    private_new = Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="PRIVATE NEW",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=mila,
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    public_ids = [message.pk for message in response.context["public_messages"]]
    assert public_ids[:2] == [public_new.pk, public_old.pk]
    private_ids = [
        item["message"].pk
        for item in response.context["player_private"][mila.pk]
    ]
    assert private_ids[:2] == [private_new.pk, private_old.pk]


@pytest.mark.django_db
def test_saoot_controls_list_latest_round_out_of_turn_declarations():
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
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        author_type=AuthorType.PLAYER,
        author_player=mila,
        content="Мила перехватывает дверь.",
        visibility=Visibility.PUBLIC,
        action_type="ACT_OUT_OF_TURN",
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        author_type=AuthorType.PLAYER,
        author_player=mathis,
        content="Матис выключает питание.",
        visibility=Visibility.PUBLIC,
        action_type="ACT_OUT_OF_TURN",
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))

    assert response.status_code == 200
    html = response.content.decode()
    assert 'id="saoot-target"' in html
    assert "SAOOT" in html
    assert f'value="{mila.pk}"' in html
    assert f'value="{mathis.pk}"' in html
    assert "Мила перехватывает дверь." in html
    assert "Матис выключает питание." in html


@pytest.mark.django_db
def test_saoot_marker_is_accepted_only_for_latest_round_declaration():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )
    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.ROUND,
        state=TurnState.COMPLETED,
        participants=[lucien.pk, mila.pk],
        active_player_id_snapshot=lucien.pk,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        author_type=AuthorType.PLAYER,
        author_player=mila,
        content="Мила закрывает дверь.",
        visibility=Visibility.PUBLIC,
        action_type="ACT_OUT_OF_TURN",
    )

    marker = f"[[SAOOT:{mila.pk}|Мила]]Дверь успевает закрыться.[[/SAOOT]]"
    accepted = Client().post(
        reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
        {"content": marker},
    )

    assert accepted.status_code == 302
    stored = Message.objects.get(
        scene=scene,
        author_type=AuthorType.GM,
        content=marker,
    )
    assert stored.visibility == Visibility.PUBLIC

    rejected = Client().post(
        reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
        {"content": f"[[SAOOT:{lucien.pk}|Люсьен]]Нет.[[/SAOOT]]"},
    )
    assert rejected.status_code == 400


@pytest.mark.django_db
def test_saoot_marker_renders_as_highlight_without_raw_markup():
    campaign = make_campaign()
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL)

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content=(
            f"[[SAOOT:{mila.pk}|Мила]]"
            "Мила успевает перехватить дверь."
            "[[/SAOOT]]"
        ),
        visibility=Visibility.PUBLIC,
    )

    response = Client().get(reverse("scene_fragment", kwargs={"scene_id": scene.pk}))

    html = response.content.decode()
    assert "saoot-resolution" in html
    assert "SAOOT · Мила" in html
    assert "Мила успевает перехватить дверь." in html
    assert "[[SAOOT:" not in html


@pytest.mark.django_db
def test_scene_displays_dialogue_language_and_followup_inherits_it():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    source = make_scene(
        campaign,
        name="French scene",
        mode=TurnMode.MANUAL,
        participants=[lucien],
        dialogue_language="French",
        is_closed=True,
    )

    page = Client().get(reverse("scene", kwargs={"scene_id": source.pk}))
    assert page.status_code == 200
    assert "Speech:" in page.content.decode()
    assert "French" in page.content.decode()

    response = Client().post(
        reverse("create_followup_scene", kwargs={"scene_id": source.pk}),
        {"name": "Next French scene", "participants": [str(lucien.pk)]},
    )

    assert response.status_code == 302
    created = source.next_scenes.get(name="Next French scene")
    assert created.dialogue_language == "French"


@pytest.mark.django_db
def test_public_scene_renders_bilingual_speech_tooltip():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content=(
            "Люсьен открывает дверь.\n"
            "[[SPEECH]]Je reviens dans une minute."
            "[[RU]]Я вернусь через минуту.[[/SPEECH]]"
        ),
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()

    assert "Je reviens dans une minute." in html
    assert 'data-translation="Я вернусь через минуту."' in html
    assert "[[SPEECH]]" not in html


@pytest.mark.django_db
def test_public_and_private_messages_have_copy_controls():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="Публичное сообщение.",
        visibility=Visibility.PUBLIC,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Личная заявка.",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=lucien,
        action_type="ACT",
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()

    assert response.status_code == 200
    assert html.count('class="copy-message-btn"') >= 2
    assert 'onclick="copyMessage(this)"' in html
    assert "async function copyMessage(button)" in html


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



@pytest.mark.django_db
def test_completed_player_reply_shows_ooc_regen_and_copy_controls():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
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
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        author_type=AuthorType.GM,
        content="Мастерский текст.",
        visibility=Visibility.PUBLIC,
    )
    player_message = Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        execution=execution,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Люсьен отвечает.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()

    assert response.status_code == 200
    assert reverse(
        "ooc_revision",
        kwargs={"scene_id": scene.pk, "message_id": player_message.pk},
    ) in html
    assert reverse(
        "regenerate_execution",
        kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
    ) in html
    assert ">OOC</button>" in html
    assert ">Regen</button>" in html
    assert "prepareOocRevision" in html


@pytest.mark.django_db
def test_ooc_revision_view_targets_exact_public_player_message():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
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
    message = Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        execution=execution,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Старая заявка.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )

    with patch("rpg.views.turn_engine.revise_execution_ooc") as revise:
        response = Client().post(
            reverse(
                "ooc_revision",
                kwargs={"scene_id": scene.pk, "message_id": message.pk},
            ),
            {"comment": "Сделай короче."},
        )

    assert response.status_code == 302
    revise.assert_called_once()
    assert revise.call_args.kwargs["public_message"].pk == message.pk
    assert revise.call_args.kwargs["gm_comment"] == "Сделай короче."


@pytest.mark.django_db
def test_regenerate_view_targets_only_requested_completed_execution():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien, mila])
    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.MANUAL,
        state=TurnState.COMPLETED,
        participants=[lucien.pk, mila.pk],
    )
    lucien_execution = TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        order_index=0,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
    )
    TurnExecution.objects.create(
        turn=turn,
        player=mila,
        order_index=1,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
    )

    with patch("rpg.views.turn_engine.regenerate_execution") as regenerate:
        response = Client().post(
            reverse(
                "regenerate_execution",
                kwargs={
                    "scene_id": scene.pk,
                    "execution_id": lucien_execution.pk,
                },
            ),
            {"message_id": "123"},
        )

    assert response.status_code == 302
    regenerate.assert_called_once()
    assert regenerate.call_args.args[0].pk == lucien_execution.pk


@pytest.mark.django_db
def test_ooc_revision_rejects_empty_comment_before_model_call():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
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
    message = Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        execution=execution,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Заявка.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )

    with patch("rpg.views.turn_engine.revise_execution_ooc") as revise:
        response = Client().post(
            reverse(
                "ooc_revision",
                kwargs={"scene_id": scene.pk, "message_id": message.pk},
            ),
            {"comment": "   "},
        )

    assert response.status_code == 400
    revise.assert_not_called()



@pytest.mark.django_db
def test_round_scene_shows_silence_button():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[lucien, mila],
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="gm-silence-button"' in html
    assert reverse("silent_turn", kwargs={"scene_id": scene.pk}) in html
    assert "Молчание" in html


@pytest.mark.django_db
def test_manual_silence_requires_exactly_one_selected_player():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[lucien, mila],
    )

    with patch("rpg.views.turn_engine.start_silent_turn") as silent:
        none_selected = Client().post(
            reverse("silent_turn", kwargs={"scene_id": scene.pk}),
            {},
        )
        two_selected = Client().post(
            reverse("silent_turn", kwargs={"scene_id": scene.pk}),
            {"selected_players": [str(lucien.pk), str(mila.pk)]},
        )
        one_selected = Client().post(
            reverse("silent_turn", kwargs={"scene_id": scene.pk}),
            {"selected_players": [str(mila.pk)], "client_turn_id": "12345678-1234-5678-1234-567812345678"},
        )

    assert none_selected.status_code == 400
    assert two_selected.status_code == 400
    assert one_selected.status_code == 302
    silent.assert_called_once()
    assert [p.pk for p in silent.call_args.kwargs["selected_players"]] == [mila.pk]


@pytest.mark.django_db
def test_round_silence_ignores_manual_player_selection_and_runs_round():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[lucien, mila],
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    with patch("rpg.views.turn_engine.start_silent_turn") as silent:
        response = Client().post(
            reverse("silent_turn", kwargs={"scene_id": scene.pk}),
            {"selected_players": [str(mila.pk)]},
        )

    assert response.status_code == 302
    silent.assert_called_once()
    assert silent.call_args.kwargs["selected_players"] is None


@pytest.mark.django_db
def test_silence_view_rejects_non_round_non_manual_mode():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(
        campaign,
        mode=TurnMode.TABLE,
        participants=[lucien],
    )

    with patch("rpg.views.turn_engine.start_silent_turn") as silent:
        response = Client().post(
            reverse("silent_turn", kwargs={"scene_id": scene.pk}),
            {"selected_players": [str(lucien.pk)]},
        )

    assert response.status_code == 400
    silent.assert_not_called()



@pytest.mark.django_db
def test_scene_exposes_ooc_mode_private_panel_controls_search_and_hotkeys():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="gm-mode-toggle"' in html
    assert 'id="ooc-target"' in html
    assert "All players" in html
    assert 'id="private-toggle"' in html
    assert 'id="private-autoopen"' in html
    assert "mraz.private.autoopen" in html
    assert 'id="public-filter-text"' in html
    assert "Ctrl" not in html  # shortcuts are behavior, not visual clutter
    assert 'event.ctrlKey && event.key === "Enter"' in html
    assert "min-height:150px" in html
    assert "minmax(440px, .9fr)" in html


@pytest.mark.django_db
def test_ooc_meta_can_target_all_or_one_player():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien, mila])

    all_response = Client().post(
        reverse("send_ooc_meta", kwargs={"scene_id": scene.pk}),
        {"content": "Это метаинформация.", "target": "all"},
    )
    assert all_response.status_code == 302
    assert Message.objects.filter(
        scene=scene,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        author_type=AuthorType.GM,
        content="[OOC META]\nЭто метаинформация.",
    ).count() == 2

    one_response = Client().post(
        reverse("send_ooc_meta", kwargs={"scene_id": scene.pk}),
        {"content": "Только Миле.", "target": str(mila.pk)},
    )
    assert one_response.status_code == 302
    only_mila = Message.objects.get(scene=scene, content="[OOC META]\nТолько Миле.")
    assert only_mila.private_player_id == mila.pk


@pytest.mark.django_db
def test_nudge_endpoint_sets_and_clears_one_shot_instruction():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    response = Client().post(
        reverse("set_player_nudge", kwargs={"scene_id": scene.pk, "player_id": lucien.pk}),
        {"content": "Не раскрывай секрет."},
    )
    assert response.status_code == 302
    lucien.refresh_from_db()
    assert lucien.pending_nudge == "Не раскрывай секрет."

    response = Client().post(
        reverse("set_player_nudge", kwargs={"scene_id": scene.pk, "player_id": lucien.pk}),
        {"content": ""},
    )
    assert response.status_code == 302
    lucien.refresh_from_db()
    assert lucien.pending_nudge == ""


@pytest.mark.django_db
def test_failed_execution_offers_fallback_retry_and_debug():
    campaign = make_campaign()
    primary = make_model("Primary UI", gateway_model="primary-ui")
    fallback = make_model("Fallback UI", gateway_model="fallback-ui")
    lucien = make_player(
        campaign,
        "Люсьен",
        model_config=primary,
        fallback_model_config=fallback,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.MANUAL,
        state=TurnState.FAILED,
        participants=[lucien.pk],
    )
    execution = TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        order_index=0,
        state=ExecutionState.FAILED,
        error="boom",
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()

    assert reverse(
        "retry_execution_fallback",
        kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
    ) in html
    assert "Retry Fallback UI" in html
    assert reverse(
        "execution_debug",
        kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
    ) in html


@pytest.mark.django_db
def test_fallback_retry_view_passes_player_fallback_model():
    campaign = make_campaign()
    fallback = make_model("Fallback Route", gateway_model="fallback-route")
    lucien = make_player(campaign, "Люсьен", fallback_model_config=fallback)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.MANUAL,
        state=TurnState.FAILED,
        participants=[lucien.pk],
    )
    execution = TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        state=ExecutionState.FAILED,
        error="timeout",
    )

    with patch("rpg.views.turn_engine.retry_execution") as retry:
        response = Client().post(
            reverse(
                "retry_execution_fallback",
                kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
            )
        )

    assert response.status_code == 302
    retry.assert_called_once()
    assert retry.call_args.kwargs["model_config"].pk == fallback.pk


@pytest.mark.django_db
def test_execution_debug_page_displays_snapshots():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    turn = Turn.objects.create(scene=scene, mode=TurnMode.MANUAL, participants=[lucien.pk])
    execution = TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
        model_used="model-x",
        system_prompt_snapshot="SYSTEM SNAPSHOT",
        request_messages=[{"role": "user", "content": "hello"}],
        raw_response='{"action_type":"ACT"}',
        latency_ms=321,
    )

    response = Client().get(
        reverse("execution_debug", kwargs={"scene_id": scene.pk, "execution_id": execution.pk})
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert "model-x" in html
    assert "321 ms" in html
    assert "SYSTEM SNAPSHOT" in html
    assert "Raw provider response" in html


@pytest.mark.django_db
def test_message_versions_page_and_restore_route():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    turn = Turn.objects.create(scene=scene, mode=TurnMode.MANUAL, participants=[lucien.pk])
    execution = TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
    )
    message = Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        execution=execution,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Current.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )
    old = MessageRevision.objects.create(
        message=message,
        revision_index=1,
        content="Old.",
        action_type="ACT",
        reason="ORIGINAL",
    )
    MessageRevision.objects.create(
        message=message,
        revision_index=2,
        content="Current.",
        action_type="ACT",
        reason="REGEN",
    )

    page = Client().get(
        reverse("message_versions", kwargs={"scene_id": scene.pk, "message_id": message.pk})
    )
    assert page.status_code == 200
    assert "Versions" not in page.content.decode() or "v2" in page.content.decode()

    restored = Client().post(
        reverse(
            "restore_message_version",
            kwargs={
                "scene_id": scene.pk,
                "message_id": message.pk,
                "revision_id": old.pk,
            },
        )
    )
    assert restored.status_code == 302
    message.refresh_from_db()
    assert message.content == "Old."


@pytest.mark.django_db
def test_pin_message_to_scene_shared_player_memory_and_lore():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    message = Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="Important.",
        visibility=Visibility.PUBLIC,
    )

    client = Client()
    for target, text in [
        ("scene", "Scene fact"),
        ("shared", "Shared fact"),
        (f"player:{lucien.pk}", "Lucien fact"),
        ("lore", "Lore fact"),
    ]:
        response = client.post(
            reverse(
                "pin_message_memory",
                kwargs={"scene_id": scene.pk, "message_id": message.pk},
            ),
            {"target": target, "content": text},
        )
        assert response.status_code == 302

    scene.refresh_from_db()
    campaign.refresh_from_db()
    lucien.refresh_from_db()
    assert "Scene fact" in scene.memory_summary
    assert "Shared fact" in campaign.shared_memory
    assert "Lucien fact" in lucien.memory_summary
    assert LoreEntry.objects.filter(campaign=campaign, content="Lore fact").exists()


@pytest.mark.django_db
def test_prepare_and_apply_close_summary_requires_gm_confirmation():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен", memory_summary="Old memory.")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    def fake_summary(target_scene):
        draft = {
            "scene_summary": "Scene draft.",
            "open_hooks": "Unresolved hook.",
            "players": {str(lucien.pk): "New player memory."},
        }
        target_scene.close_summary_draft = draft
        target_scene.save(update_fields=["close_summary_draft", "updated_at"])
        return draft

    with patch("rpg.views.generate_close_summary", side_effect=fake_summary):
        response = Client().post(
            reverse("prepare_close_summary", kwargs={"scene_id": scene.pk})
        )

    assert response.status_code == 302
    scene.refresh_from_db()
    assert scene.is_closed is False
    assert scene.close_summary_draft["scene_summary"] == "Scene draft."

    page = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = page.content.decode()
    assert "Close-scene review" in html
    assert "Scene draft." in html
    assert "New player memory." in html

    applied = Client().post(
        reverse("apply_close_summary", kwargs={"scene_id": scene.pk}),
        {
            "scene_summary": "Edited scene summary.",
            "open_hooks": "Edited hook.",
            f"player_memory_{lucien.pk}": "Edited player memory.",
        },
    )
    assert applied.status_code == 302
    scene.refresh_from_db()
    lucien.refresh_from_db()
    assert scene.is_closed is True
    assert "Edited scene summary." in scene.memory_summary
    assert "Edited hook." in scene.memory_summary
    assert "Old memory." in lucien.memory_summary
    assert "Edited player memory." in lucien.memory_summary


@pytest.mark.django_db
def test_private_channel_fragment_contains_both_sides_without_replacing_composer():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="GM private",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=lucien,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Player private",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=lucien,
        gm_unread=True,
    )

    fragment = Client().get(
        reverse("private_channel", kwargs={"scene_id": scene.pk, "player_id": lucien.pk})
    )
    html = fragment.content.decode()
    assert fragment.status_code == 200
    assert "GM private" in html
    assert "Player private" in html
    assert 'name="content"' not in html

    scene_page = Client().get(reverse("scene", kwargs={"scene_id": scene.pk})).content.decode()
    assert 'placeholder="Private to Люсьен..."' in scene_page
    assert "hx-trigger=\"load, every 3s\"" in scene_page



@pytest.mark.django_db
def test_history_search_spans_predecessor_scenes_and_filters_author_action():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    old_scene = make_scene(
        campaign,
        name="Old scene",
        mode=TurnMode.MANUAL,
        participants=[lucien, mila],
    )

    Message.objects.create(
        campaign=campaign,
        scene=old_scene,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Старый след про Сибиллу.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )
    old_scene.is_closed = True
    old_scene.save(update_fields=["is_closed", "updated_at"])

    new_scene = make_scene(
        campaign,
        name="New scene",
        mode=TurnMode.MANUAL,
        participants=[lucien, mila],
        predecessors=[old_scene],
    )
    Message.objects.create(
        campaign=campaign,
        scene=new_scene,
        author_type=AuthorType.PLAYER,
        author_player=mila,
        content="Новая реплика.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )

    response = Client().get(
        reverse("search_history", kwargs={"scene_id": new_scene.pk}),
        {
            "q": "Сибиллу",
            "author": f"player:{lucien.pk}",
            "action": "ACT",
            "visibility": "PUBLIC",
        },
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert "Старый след про Сибиллу." in html
    assert "Old scene" in html
    assert "Новая реплика." not in html



@pytest.mark.django_db
def test_manual_chat_player_card_shows_copy_open_and_paste_controls():
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Люсьен",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_label="ChatGPT",
        manual_chat_url="https://chatgpt.com/c/example",
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="Что делаешь?",
        selected_players=[lucien],
    )
    execution = result.turn.executions.get(player=lucien)

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()

    assert response.status_code == 200
    assert "External response needed" in html
    assert "BOOTSTRAP" in html
    assert "Copy prompt" in html
    assert "Open chat" in html
    assert "https://chatgpt.com/c/example" in html
    assert "Send via browser bridge" in html
    assert 'data-mraz-bridge-kind="player"' in html
    assert f'data-mraz-bridge-execution="{execution.pk}"' in html
    assert "data-mraz-bridge-prompt" in html
    assert "data-mraz-bridge-response-form" in html
    assert "Accept pasted response" in html
    assert reverse(
        "submit_external_response",
        kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
    ) in html


@pytest.mark.django_db
def test_manual_chat_submit_auto_continues_to_manual_chat_gm():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    ai_player = make_player(
        campaign,
        "Виктория",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_label="ChatGPT Player",
        manual_chat_url="https://chatgpt.com/c/test-player",
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
    )
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[human, ai_player],
    )
    scene.round_order = [human.pk, ai_player.pk]
    scene.active_player_index = 0
    scene.save(update_fields=["round_order", "active_player_index", "updated_at"])

    GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.MANUAL_CHAT,
        review_before_publish=False,
        auto_continue=True,
        manual_chat_label="ChatGPT GM",
        manual_chat_url="https://chatgpt.com/c/test-gm",
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
    )

    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="Ваш ход.",
    )
    human_execution = result.turn.executions.get(player=human)
    ai_execution = result.turn.executions.get(player=ai_player)
    client = Client()

    human_response = client.post(
        _human_url(
            "submit_human_response",
            scene,
            human,
            execution_id=human_execution.pk,
        ),
        {"action_type": "ACT", "content": "Нед действует."},
    )

    assert human_response.status_code == 302
    assert not scene.gm_executions.exists()

    ai_response = client.post(
        reverse(
            "submit_external_response",
            kwargs={"scene_id": scene.pk, "execution_id": ai_execution.pk},
        ),
        {
            "response": (
                '{"action_type":"PASS","public":"Виктория пропускает ход.",'
                '"private_to_gm":""}'
            )
        },
    )

    assert ai_response.status_code == 302
    ai_execution.refresh_from_db()
    assert ai_execution.state == ExecutionState.COMPLETED

    gm_execution = scene.gm_executions.get()
    assert gm_execution.state == GameMasterExecutionState.WAITING_EXTERNAL
    assert gm_execution.external_chat_url == "https://chatgpt.com/c/test-gm"


@pytest.mark.django_db
def test_external_response_view_imports_valid_paste():
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Люсьен",
        transport=PlayerTransport.MANUAL_CHAT,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[lucien],
    )
    execution = result.turn.executions.get(player=lucien)

    response = Client().post(
        reverse(
            "submit_external_response",
            kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
        ),
        {
            "response": (
                '{"action_type":"ACT","public":"Люсьен отвечает.",'
                '"private_to_gm":""}'
            )
        },
    )

    assert response.status_code == 302
    execution.refresh_from_db()
    assert execution.state == ExecutionState.COMPLETED
    assert Message.objects.filter(
        execution=execution,
        content="Люсьен отвечает.",
        visibility=Visibility.PUBLIC,
    ).exists()


@pytest.mark.django_db
def test_external_response_view_redirects_and_keeps_rejection_on_execution():
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Люсьен",
        transport=PlayerTransport.MANUAL_CHAT,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[lucien],
    )
    execution = result.turn.executions.get(player=lucien)

    response = Client().post(
        reverse(
            "submit_external_response",
            kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
        ),
        {"response": '{"action_type":"ACT","public":"' + ("x" * 1201) + '"}'},
    )

    assert response.status_code == 302
    execution.refresh_from_db()
    assert execution.state == ExecutionState.WAITING_EXTERNAL
    assert "too long" in execution.error


@pytest.mark.django_db
def test_reset_manual_chat_memory_forces_next_bootstrap():
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Люсьен",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
        manual_chat_initialized=True,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    response = Client().post(
        reverse(
            "reset_manual_chat_memory",
            kwargs={"scene_id": scene.pk, "player_id": lucien.pk},
        )
    )

    assert response.status_code == 302
    lucien.refresh_from_db()
    assert lucien.manual_chat_initialized is False



@pytest.mark.django_db
def test_manual_chat_paste_box_is_preserved_across_player_polling():
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Люсьен",
        transport=PlayerTransport.MANUAL_CHAT,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[lucien],
    )
    execution = result.turn.executions.get(player=lucien)

    response = Client().get(
        reverse("players_status", kwargs={"scene_id": scene.pk})
    )
    html = response.content.decode()

    assert response.status_code == 200
    assert f'id="external-response-{execution.pk}"' in html
    assert "hx-preserve" in html


@pytest.mark.django_db
def test_manual_chat_public_reply_hides_api_only_ooc_and_regen_controls():
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Люсьен",
        transport=PlayerTransport.MANUAL_CHAT,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[lucien],
    )
    execution = result.turn.executions.get(player=lucien)
    turn_engine.submit_external_response(
        execution=execution,
        raw_text='{"action_type":"ACT","public":"Ответ.","private_to_gm":""}',
    )

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Versions / restore" in html
    assert reverse(
        "execution_debug",
        kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
    ) in html
    assert reverse(
        "regenerate_execution",
        kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
    ) not in html



@pytest.mark.django_db
def test_invalid_execution_labels_retry_as_corrective():
    campaign = make_campaign()
    fallback = make_model("Fallback Fix", gateway_model="fallback-fix")
    mathis = make_player(
        campaign,
        "Матис",
        fallback_model_config=fallback,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[mathis])
    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.MANUAL,
        state=TurnState.FAILED,
        participants=[mathis.pk],
    )
    TurnExecution.objects.create(
        turn=turn,
        player=mathis,
        state=ExecutionState.INVALID,
        error="forbidden action",
    )

    html = Client().get(
        reverse("scene", kwargs={"scene_id": scene.pk})
    ).content.decode()

    assert "Retry + fix" in html
    assert "Retry + fix Fallback Fix" in html



def _human_token(scene, player):
    return scene.scene_participants.get(player=player).human_access_token


def _human_url(name, scene, player, **kwargs):
    params = {"access_token": _human_token(scene, player)}
    params.update(kwargs)
    return reverse(name, kwargs=params)


@pytest.mark.django_db
def test_human_player_client_shows_only_public_and_own_private_messages():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    other = make_player(campaign, "Другой")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human, other])

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="PUBLIC_VISIBLE",
        visibility=Visibility.PUBLIC,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="OWN_PRIVATE_VISIBLE",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=human,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="OTHER_PRIVATE_SECRET",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=other,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="GM_ONLY_SECRET",
        visibility=Visibility.GM_ONLY,
    )

    response = Client().get(_human_url("human_player_client", scene, human))
    html = response.content.decode()

    assert response.status_code == 200
    assert "PUBLIC_VISIBLE" in html
    assert "OWN_PRIVATE_VISIBLE" in html
    assert "OTHER_PRIVATE_SECRET" not in html
    assert "GM_ONLY_SECRET" not in html
    assert 'href="/admin/"' not in html
    assert response["Cache-Control"] == "private, no-store"
    assert response["Referrer-Policy"] == "no-referrer"
    assert response["X-Robots-Tag"] == "noindex, nofollow"


@pytest.mark.django_db
def test_human_player_client_shows_round_legal_actions_only(mock_backend):
    campaign = make_campaign()
    api = make_player(campaign, "API")
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[api, human],
        round_order=[api.pk, human.pk],
        active_player_index=0,
    )

    with patch(
        "rpg.services.turn_engine.get_llm_client",
        return_value=MockLLMClient(),
    ):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")
    execution = result.turn.executions.get(player=human)

    response = Client().get(_human_url("human_player_client", scene, human))
    html = response.content.decode()

    assert response.status_code == 200
    assert (
        f'action="{_human_url("submit_human_response", scene, human, execution_id=execution.pk)}"'
        in html
    )
    assert '<option value="PASS">PASS</option>' in html
    assert '<option value="ACT_OUT_OF_TURN">ACT_OUT_OF_TURN</option>' in html
    assert '<option value="ACT">ACT</option>' not in html


@pytest.mark.django_db
def test_human_submit_view_completes_waiting_execution(mock_backend):
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[human],
    )
    execution = result.turn.executions.get(player=human)

    response = Client().post(
        _human_url(
            "submit_human_response",
            scene,
            human,
            execution_id=execution.pk,
        ),
        {"action_type": "ACT", "content": "Я отвечаю.", "private_to_gm": ""},
    )

    assert response.status_code == 302
    assert response["Location"] == _human_url("human_player_client", scene, human)
    execution.refresh_from_db()
    assert execution.state == ExecutionState.COMPLETED
    assert Message.objects.filter(
        execution=execution,
        visibility=Visibility.PUBLIC,
        content="Я отвечаю.",
    ).exists()


@pytest.mark.django_db
def test_human_bearer_token_post_does_not_require_csrf():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[human],
    )
    execution = result.turn.executions.get(player=human)
    csrf_client = Client(enforce_csrf_checks=True)

    response = csrf_client.post(
        _human_url(
            "submit_human_response",
            scene,
            human,
            execution_id=execution.pk,
        ),
        {"action_type": "ACT", "content": "Я отвечаю без CSRF cookie."},
    )

    assert response.status_code == 302
    execution.refresh_from_db()
    assert execution.state == ExecutionState.COMPLETED


@pytest.mark.django_db
def test_csrf_remains_enabled_for_master_write_routes():
    campaign = make_campaign()
    player = make_player(campaign, "P")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    csrf_client = Client(enforce_csrf_checks=True)

    response = csrf_client.post(
        reverse("start_model_gm", kwargs={"scene_id": scene.pk})
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_human_submit_auto_continues_to_manual_chat_gm():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.MANUAL_CHAT,
        review_before_publish=False,
        auto_continue=True,
        manual_chat_label="ChatGPT GM",
        manual_chat_url="https://chatgpt.com/c/test-gm",
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
    )
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="Твой ход.",
        selected_players=[human],
    )
    execution = result.turn.executions.get(player=human)
    client = Client()

    response = client.post(
        _human_url(
            "submit_human_response",
            scene,
            human,
            execution_id=execution.pk,
        ),
        {"action_type": "ACT", "content": "Я осматриваю вошедшего мужчину."},
    )

    assert response.status_code == 302
    execution.refresh_from_db()
    assert execution.state == ExecutionState.COMPLETED

    gm_execution = scene.gm_executions.get()
    assert gm_execution.state == GameMasterExecutionState.WAITING_EXTERNAL
    assert gm_execution.external_chat_url == "https://chatgpt.com/c/test-gm"

    human_html = client.get(
        _human_url("human_player_client", scene, human)
    ).content.decode()
    assert "MASTER THINKING" in human_html

    gm_html = client.get(
        reverse("scene", kwargs={"scene_id": scene.pk})
    ).content.decode()
    assert 'data-mraz-gm-autoplay="1"' in gm_html
    assert 'data-mraz-bridge-autostart="1"' in gm_html

    gm_execution.error = "Rejected response"
    gm_execution.save(update_fields=["error", "updated_at"])
    paused_html = client.get(
        reverse("scene", kwargs={"scene_id": scene.pk})
    ).content.decode()
    assert 'data-mraz-gm-autoplay="1"' in paused_html
    assert 'data-mraz-bridge-autostart="1"' not in paused_html


@pytest.mark.django_db
def test_human_access_token_cannot_submit_another_players_execution():
    campaign = make_campaign()
    human_a = make_player(campaign, "A", transport=PlayerTransport.HUMAN)
    human_b = make_player(campaign, "B", transport=PlayerTransport.HUMAN)
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[human_a, human_b],
    )
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[human_b],
    )
    b_execution = result.turn.executions.get(player=human_b)

    response = Client().post(
        _human_url(
            "submit_human_response",
            scene,
            human_a,
            execution_id=b_execution.pk,
        ),
        {"action_type": "ACT", "content": "Попытка подмены."},
    )

    assert response.status_code == 404
    b_execution.refresh_from_db()
    assert b_execution.state == ExecutionState.WAITING_HUMAN


@pytest.mark.django_db
def test_human_ooc_is_private_and_unread_for_gm():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    response = Client().post(
        _human_url("human_send_ooc", scene, human),
        {"content": "А это точно хорошая идея?"},
    )

    assert response.status_code == 302
    message = Message.objects.get(
        scene=scene,
        author_player=human,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=human,
    )
    assert message.content == "[OOC PLAYER]\nА это точно хорошая идея?"
    assert message.gm_unread is True


@pytest.mark.django_db
def test_gm_player_card_exposes_secret_human_link_and_management_controls(mock_backend):
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    participation = scene.scene_participants.get(player=human)

    response = Client().get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = response.content.decode()
    client_url = _human_url("human_player_client", scene, human)

    assert response.status_code == 200
    assert "transport: human player" in html
    assert str(participation.human_access_token) in html
    assert client_url in html
    assert "Copy link" in html
    assert "Regenerate" in html
    assert "Revoke" in html
    assert "Open player client" in html
    assert reverse(
        "regenerate_human_access",
        kwargs={"scene_id": scene.pk, "player_id": human.pk},
    ) in html
    assert reverse(
        "toggle_human_access",
        kwargs={"scene_id": scene.pk, "player_id": human.pk},
    ) in html


@pytest.mark.django_db
def test_human_access_can_be_revoked_enabled_and_regenerated():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    client = Client()
    participation = scene.scene_participants.get(player=human)
    original_token = participation.human_access_token
    original_url = reverse(
        "human_player_client",
        kwargs={"access_token": original_token},
    )

    assert client.get(original_url).status_code == 200

    revoke = client.post(
        reverse(
            "toggle_human_access",
            kwargs={"scene_id": scene.pk, "player_id": human.pk},
        )
    )
    assert revoke.status_code == 302
    participation.refresh_from_db()
    assert participation.human_access_enabled is False
    assert client.get(original_url).status_code == 404

    enable = client.post(
        reverse(
            "toggle_human_access",
            kwargs={"scene_id": scene.pk, "player_id": human.pk},
        )
    )
    assert enable.status_code == 302
    participation.refresh_from_db()
    assert participation.human_access_enabled is True
    assert client.get(original_url).status_code == 200

    regenerated = client.post(
        reverse(
            "regenerate_human_access",
            kwargs={"scene_id": scene.pk, "player_id": human.pk},
        )
    )
    assert regenerated.status_code == 302
    participation.refresh_from_db()
    assert participation.human_access_enabled is True
    assert participation.human_access_token != original_token
    assert client.get(original_url).status_code == 404
    assert client.get(
        reverse(
            "human_player_client",
            kwargs={"access_token": participation.human_access_token},
        )
    ).status_code == 200


@pytest.mark.django_db
def test_old_scene_token_cannot_expose_later_episode():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    old_scene = make_scene(
        campaign,
        name="Old",
        mode=TurnMode.MANUAL,
        participants=[human],
    )
    old_token = _human_token(old_scene, human)

    later_scene = make_scene(
        campaign,
        name="Later secret episode",
        mode=TurnMode.MANUAL,
        participants=[human],
    )
    Message.objects.create(
        campaign=campaign,
        scene=later_scene,
        author_type=AuthorType.GM,
        content="FUTURE_PRIVATE_SECRET",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=human,
    )

    search_response = Client().get(
        reverse("human_episode_search", kwargs={"access_token": old_token})
    )
    search_html = search_response.content.decode()
    assert "Later secret episode" not in search_html

    detail_response = Client().get(
        reverse(
            "human_episode_detail",
            kwargs={
                "access_token": old_token,
                "episode_id": later_scene.pk,
            },
        )
    )
    assert detail_response.status_code == 404


@pytest.mark.django_db
def test_old_id_based_human_client_url_is_not_routed():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    response = Client().get(f"/scene/{scene.pk}/human/{human.pk}/")

    assert response.status_code == 404


@pytest.mark.django_db
def test_human_declaration_hides_model_regen_and_ooc_revision_controls(mock_backend):
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[human],
    )
    execution = result.turn.executions.get(player=human)
    turn_engine.submit_human_response(
        execution=execution,
        action_type="ACT",
        public_text="Человеческая заявка.",
    )
    message = Message.objects.get(
        execution=execution,
        visibility=Visibility.PUBLIC,
        author_type=AuthorType.PLAYER,
    )

    html = Client().get(reverse("scene", kwargs={"scene_id": scene.pk})).content.decode()

    assert reverse(
        "regenerate_execution",
        kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
    ) not in html
    assert reverse(
        "ooc_revision",
        kwargs={"scene_id": scene.pk, "message_id": message.pk},
    ) not in html
    assert "Versions / restore" in html


@pytest.mark.django_db
def test_human_client_preserves_disclosures_scroll_and_focus_across_polling():
    campaign = make_campaign()
    human = make_player(
        campaign,
        "Живой",
        transport=PlayerTransport.HUMAN,
        memory_summary="Помнит важную вещь.",
    )
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[human],
        description="Длинная сцена.",
    )

    response = Client().get(_human_url("human_player_client", scene, human))
    html = response.content.decode()

    assert response.status_code == 200
    assert 'id="human-notes"' in html
    assert 'data-human-preserve-open="notes"' in html
    assert 'id="human-public-scroll"' in html
    assert 'data-human-workspace-scroll="scene"' in html
    assert 'id="human-private-scroll"' in html
    assert 'data-human-workspace-scroll="private"' in html
    assert 'hx-trigger="human-scene-poll"' in html
    assert 'hx-trigger="human-private-poll"' in html
    assert "htmx:beforeSwap" in html
    assert "htmx:afterSwap" in html
    assert "humanCaptureRegion" in html
    assert "humanRestoreRegion" in html
    assert "humanRegionIsEditing" in html
    assert "humanPreserveEvictedMessages" in html
    assert "humanCompactHistory" in html
    assert "window.setInterval(humanPollAll, 2000)" in html
    assert "selectionStart" in html
    assert "sessionStorage" in html


@pytest.mark.django_db
def test_master_scene_uses_gm_favicon():
    campaign = make_campaign()
    player = make_player(campaign, "Игрок")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])

    html = Client().get(reverse("scene", kwargs={"scene_id": scene.pk})).content.decode()

    assert 'href="/static/rpg/favicon-gm.svg"' in html
    assert 'href="/static/rpg/favicon-player.svg"' not in html
    assert 'href="/static/rpg/favicon-admin.svg"' not in html


@pytest.mark.django_db
def test_human_client_uses_player_favicon():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    html = Client().get(
        _human_url("human_player_client", scene, human)
    ).content.decode()

    assert 'href="/static/rpg/favicon-player.svg"' in html
    assert 'href="/static/rpg/favicon-gm.svg"' not in html
    assert 'href="/static/rpg/favicon-admin.svg"' not in html


@pytest.mark.django_db
def test_admin_uses_admin_favicon():
    admin = get_user_model().objects.create_superuser(
        username="favicon-admin",
        email="favicon@example.test",
        password="test-password",
    )
    client = Client()
    client.force_login(admin)

    with override_settings(
        STORAGES={
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
            },
        }
    ):
        html = client.get(reverse("admin:index")).content.decode()

    assert 'href="/static/rpg/favicon-admin.svg"' in html
    assert 'href="/static/rpg/favicon-gm.svg"' not in html
    assert 'href="/static/rpg/favicon-player.svg"' not in html


def test_favicon_static_assets_are_discoverable():
    assert finders.find("rpg/favicon-gm.svg")
    assert finders.find("rpg/favicon-player.svg")
    assert finders.find("rpg/favicon-admin.svg")


@pytest.mark.django_db
def test_human_client_renders_character_card_and_episode_search():
    campaign = make_campaign()
    human = make_player(
        campaign,
        "Живой",
        transport=PlayerTransport.HUMAN,
        character_summary="Бывший архивист, теперь охотник на аномалии.",
        characteristics="Сила 2\nЛовкость 4\nВоля 5",
        abilities="Видит следы магии\nЗнает старые языки",
        memory_summary="Помнит встречу у старого вокзала.",
    )
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[human],
        description="Текущая сцена.",
    )

    html = Client().get(
        _human_url("human_player_client", scene, human)
    ).content.decode()

    assert "Character" in html
    assert "Бывший архивист" in html
    assert "Сила 2" in html
    assert "Видит следы магии" in html
    assert "Помнит встречу у старого вокзала" in html
    assert _human_url("human_episode_search", scene, human) in html
    assert _human_url("upload_human_character_image", scene, human) in html


@pytest.mark.django_db
def test_human_client_puts_gameplay_before_reference_and_collapses_character_sections():
    campaign = make_campaign()
    human = make_player(
        campaign,
        "Живой",
        transport=PlayerTransport.HUMAN,
        characteristics="Сила 2",
        abilities="Видит следы магии",
        memory_summary="Помнит старый вокзал.",
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    html = Client().get(
        _human_url("human_player_client", scene, human)
    ).content.decode()

    primary_index = html.index('id="human-character-primary"')
    scene_index = html.index('id="human-workspace-scene"')
    private_index = html.index('id="human-workspace-private"')
    reference_index = html.index('id="human-character-reference"')

    assert primary_index < scene_index < private_index < reference_index
    assert 'data-human-workspace-target="scene"' in html
    assert 'data-human-workspace-target="private"' in html
    assert 'data-human-workspace-target="character"' in html
    assert not re.search(
        r"<details[^>]*\bopen\b[^>]*>\s*<summary>Characteristics</summary>",
        html,
    )
    assert not re.search(
        r"<details[^>]*\bopen\b[^>]*>\s*<summary>Abilities</summary>",
        html,
    )
    assert not re.search(
        r"<details[^>]*\bopen\b[^>]*>\s*<summary>Memory summary</summary>",
        html,
    )
    assert "height:100dvh" in html
    assert "grid-template-columns:72px minmax(0,1fr)" in html


@pytest.mark.django_db
def test_human_client_paginates_newest_first_and_filters_empty_none_messages():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    created = []
    for index in range(75):
        created.append(
            Message.objects.create(
                campaign=campaign,
                scene=scene,
                author_type=AuthorType.GM,
                visibility=Visibility.PUBLIC,
                content=f"PUBLIC_{index:03d}",
            )
        )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC,
        content="None",
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC,
        content="   ",
    )

    client = Client()
    html = client.get(_human_url("human_player_client", scene, human)).content.decode()

    assert "PUBLIC_074" in html
    assert "PUBLIC_015" in html
    assert "PUBLIC_014" not in html
    assert ">None<" not in html
    assert html.index("PUBLIC_074") < html.index("PUBLIC_073")
    assert "human-history-sentinel" in html

    before = created[15].pk
    older = client.get(
        _human_url("human_feed_page", scene, human, channel="public"),
        {"before": before},
    )
    older_html = older.content.decode()
    assert older.status_code == 200
    assert "PUBLIC_014" in older_html
    assert "PUBLIC_000" in older_html
    assert older_html.index("PUBLIC_014") < older_html.index("PUBLIC_013")


@pytest.mark.django_db
def test_human_private_feed_is_separate_workspace_and_paginates():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    private_messages = []
    for index in range(65):
        private_messages.append(
            Message.objects.create(
                campaign=campaign,
                scene=scene,
                author_type=AuthorType.GM,
                visibility=Visibility.PRIVATE_GM_PLAYER,
                private_player=human,
                content=f"PRIVATE_{index:03d}",
            )
        )

    client = Client()
    html = client.get(_human_url("human_player_client", scene, human)).content.decode()

    assert 'id="human-workspace-private"' in html
    assert "Visible only to you and the Game Master" in html
    assert "PRIVATE_064" in html
    assert "PRIVATE_004" not in html
    assert "Send OOC" in html

    older = client.get(
        _human_url("human_feed_page", scene, human, channel="private"),
        {"before": private_messages[5].pk},
    )
    older_html = older.content.decode()
    assert "PRIVATE_004" in older_html
    assert "PRIVATE_000" in older_html


@pytest.mark.django_db
def test_human_reference_uses_one_primary_history_search():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    html = Client().get(_human_url("human_player_client", scene, human)).content.decode()

    search_url = _human_url("human_episode_search", scene, human)
    assert html.count(f'action="{search_url}"') == 1
    assert "Search episodes" not in html
    assert 'placeholder="Search visible history..."' in html


@pytest.mark.django_db
def test_human_can_upload_replace_and_remove_character_portrait(tmp_path):
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    client = Client()

    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    upload_url = _human_url("upload_human_character_image", scene, human)

    with override_settings(MEDIA_ROOT=tmp_path):
        response = client.post(
            upload_url,
            {
                "image": SimpleUploadedFile(
                    "portrait.png",
                    png_bytes,
                    content_type="image/png",
                )
            },
        )

        assert response.status_code == 302
        human.refresh_from_db()
        assert human.character_image.name.endswith(".png")

        image_response = client.get(
            _human_url("human_character_image", scene, human)
        )
        assert image_response.status_code == 200
        assert image_response["Content-Type"] == "image/png"

        old_name = human.character_image.name
        second = client.post(
            upload_url,
            {
                "image": SimpleUploadedFile(
                    "replacement.png",
                    png_bytes,
                    content_type="image/png",
                )
            },
        )
        assert second.status_code == 302
        human.refresh_from_db()
        assert human.character_image.name != old_name
        assert not (tmp_path / old_name).exists()

        removed = client.post(
            _human_url("remove_human_character_image", scene, human)
        )
        assert removed.status_code == 302
        human.refresh_from_db()
        assert not human.character_image


@pytest.mark.django_db
def test_human_portrait_rejects_non_image_upload(tmp_path):
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    with override_settings(MEDIA_ROOT=tmp_path):
        response = Client().post(
            _human_url("upload_human_character_image", scene, human),
            {
                "image": SimpleUploadedFile(
                    "not-an-image.txt",
                    b"<script>alert(1)</script>",
                    content_type="text/plain",
                )
            },
        )

    assert response.status_code == 400
    human.refresh_from_db()
    assert not human.character_image


@pytest.mark.django_db
def test_human_episode_search_is_participant_and_visibility_scoped():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    other = make_player(campaign, "Другой")

    public_episode = make_scene(
        campaign,
        name="Warehouse",
        mode=TurnMode.MANUAL,
        participants=[human, other],
        description="Старый склад.",
        memory_summary="SUMMARY_NEEDLE: группа нашла скрытый вход.",
    )
    private_episode = make_scene(
        campaign,
        name="Apartment",
        mode=TurnMode.MANUAL,
        participants=[human, other],
    )
    outsider_episode = make_scene(
        campaign,
        name="Forbidden Archive",
        mode=TurnMode.MANUAL,
        participants=[other],
    )
    current = make_scene(
        campaign,
        name="Current",
        mode=TurnMode.MANUAL,
        participants=[human, other],
    )

    Message.objects.create(
        campaign=campaign,
        scene=public_episode,
        author_type=AuthorType.GM,
        content="VISIBLE_PUBLIC_NEEDLE",
        visibility=Visibility.PUBLIC,
    )
    Message.objects.create(
        campaign=campaign,
        scene=private_episode,
        author_type=AuthorType.GM,
        content="OWN_PRIVATE_NEEDLE",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=human,
    )
    Message.objects.create(
        campaign=campaign,
        scene=private_episode,
        author_type=AuthorType.GM,
        content="OTHER_PRIVATE_NEEDLE",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=other,
    )
    Message.objects.create(
        campaign=campaign,
        scene=public_episode,
        author_type=AuthorType.GM,
        content="GM_ONLY_NEEDLE",
        visibility=Visibility.GM_ONLY,
    )

    client = Client()
    search_url = _human_url("human_episode_search", current, human)

    public_html = client.get(search_url, {"q": "VISIBLE_PUBLIC_NEEDLE"}).content.decode()
    assert "Warehouse" in public_html
    assert "Apartment" not in public_html

    summary_html = client.get(search_url, {"q": "SUMMARY_NEEDLE"}).content.decode()
    assert "Warehouse" in summary_html
    assert "группа нашла скрытый вход" in summary_html

    own_private_html = client.get(search_url, {"q": "OWN_PRIVATE_NEEDLE"}).content.decode()
    assert "Apartment" in own_private_html

    other_private_html = client.get(search_url, {"q": "OTHER_PRIVATE_NEEDLE"}).content.decode()
    assert "Apartment" not in other_private_html

    gm_only_html = client.get(search_url, {"q": "GM_ONLY_NEEDLE"}).content.decode()
    assert "Warehouse" not in gm_only_html

    outsider_html = client.get(search_url, {"q": "Forbidden Archive"}).content.decode()
    outsider_detail_url = _human_url(
        "human_episode_detail",
        current,
        human,
        episode_id=outsider_episode.pk,
    )
    assert outsider_detail_url not in outsider_html

    all_html = client.get(search_url).content.decode()
    assert "Warehouse" in all_html
    assert "Apartment" in all_html
    assert "Current" in all_html
    assert "Forbidden Archive" not in all_html


@pytest.mark.django_db
def test_human_episode_detail_shows_only_public_and_own_private_history():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    other = make_player(campaign, "Другой")
    episode = make_scene(
        campaign,
        name="Past episode",
        mode=TurnMode.MANUAL,
        participants=[human, other],
        memory_summary="EPISODE_SUMMARY_VISIBLE",
    )
    current = make_scene(
        campaign,
        name="Current",
        mode=TurnMode.MANUAL,
        participants=[human, other],
    )

    Message.objects.create(
        campaign=campaign,
        scene=episode,
        author_type=AuthorType.GM,
        content="VISIBLE_PUBLIC",
        visibility=Visibility.PUBLIC,
    )
    Message.objects.create(
        campaign=campaign,
        scene=episode,
        author_type=AuthorType.GM,
        content="VISIBLE_OWN_PRIVATE",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=human,
    )
    Message.objects.create(
        campaign=campaign,
        scene=episode,
        author_type=AuthorType.GM,
        content="HIDDEN_OTHER_PRIVATE",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=other,
    )
    Message.objects.create(
        campaign=campaign,
        scene=episode,
        author_type=AuthorType.GM,
        content="HIDDEN_GM_ONLY",
        visibility=Visibility.GM_ONLY,
    )

    html = Client().get(
        _human_url(
            "human_episode_detail",
            current,
            human,
            episode_id=episode.pk,
        )
    ).content.decode()

    assert "EPISODE_SUMMARY_VISIBLE" in html
    assert "VISIBLE_PUBLIC" in html
    assert "VISIBLE_OWN_PRIVATE" in html
    assert "HIDDEN_OTHER_PRIVATE" not in html
    assert "HIDDEN_GM_ONLY" not in html


@pytest.mark.django_db
def test_human_client_can_browse_and_set_character_appearances():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    human_form = CharacterAppearance.objects.create(
        player=human,
        name="Человеческий",
        description="Обычный человеческий облик.",
        is_primary=True,
        order=0,
    )
    true_form = CharacterAppearance.objects.create(
        player=human,
        name="Истинный",
        description="Крылья, светящиеся глаза и знаки Света.",
        order=1,
    )

    client = Client()
    base_url = _human_url("human_player_client", scene, human)

    html = client.get(base_url).content.decode()
    assert "Человеческий" in html
    assert "Истинный" in html
    assert "Обычный человеческий облик." in html
    assert "CURRENT FORM" in html

    alternate_html = client.get(base_url, {"appearance": true_form.pk}).content.decode()
    assert "Крылья, светящиеся глаза и знаки Света." in alternate_html
    assert _human_url(
        "set_human_current_appearance",
        scene,
        human,
        appearance_id=true_form.pk,
    ) in alternate_html

    response = client.post(
        _human_url(
            "set_human_current_appearance",
            scene,
            human,
            appearance_id=true_form.pk,
        )
    )
    assert response.status_code == 302
    participation = scene.scene_participants.get(player=human)
    assert participation.current_appearance_id == true_form.pk
    assert human_form.pk != true_form.pk


@pytest.mark.django_db
def test_human_token_cannot_set_another_players_appearance():
    campaign = make_campaign()
    human = make_player(campaign, "Живой", transport=PlayerTransport.HUMAN)
    other = make_player(campaign, "Другой")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human, other])
    foreign_form = CharacterAppearance.objects.create(
        player=other,
        name="Чужой облик",
        is_primary=True,
    )

    response = Client().post(
        _human_url(
            "set_human_current_appearance",
            scene,
            human,
            appearance_id=foreign_form.pk,
        )
    )
    assert response.status_code == 404
