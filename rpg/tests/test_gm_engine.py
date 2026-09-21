"""Regression tests for the model Game Master actor."""
import json
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from rpg.models import (
    AuthorType,
    CharacterAppearance,
    ExecutionState,
    GameMasterAction,
    GameMasterConfig,
    GameMasterExecutionState,
    GameMasterTransport,
    LoreEntry,
    ManualChatContextMode,
    Message,
    PlayerTransport,
    TurnMode,
    Visibility,
)
from rpg.services import gm_engine
from rpg.services.gm_context import build_gm_context
from rpg.services.llm import LLMResponse, MockLLMClient
from rpg.tests.factories import make_campaign, make_model, make_player, make_scene


class StaticGMClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def generate(self, *, system_prompt, messages, model, temperature=0.7):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "messages": list(messages),
                "model": model,
                "temperature": temperature,
            }
        )
        raw = json.dumps(self.payload, ensure_ascii=False)
        return LLMResponse(raw_text=raw, public=str(self.payload.get("public", "")))

    def close(self):
        return None


@pytest.mark.django_db
def test_gm_parser_validates_scene_player_ids_and_manual_targets():
    campaign = make_campaign()
    a = make_player(campaign, "A")
    b = make_player(campaign, "B")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[a])

    parsed = gm_engine.parse_gm_response(
        json.dumps(
            {
                "action": "TURN",
                "public": "Что-то происходит.",
                "private": [{"player_id": a.pk, "content": "Только ты это заметил."}],
                "turn_targets": [a.pk],
            },
            ensure_ascii=False,
        ),
        scene=scene,
    )
    assert parsed.action == GameMasterAction.TURN
    assert parsed.turn_targets == [a.pk]

    with pytest.raises(ValidationError, match="outside this scene"):
        gm_engine.parse_gm_response(
            json.dumps(
                {
                    "action": "TURN",
                    "public": "Что-то происходит.",
                    "private": [],
                    "turn_targets": [b.pk],
                }
            ),
            scene=scene,
        )


@pytest.mark.django_db
def test_existing_source_lookup_marks_missing_exact_fact_as_unknown():
    campaign = make_campaign()
    human = make_player(
        campaign,
        "Баальтаз",
        transport=PlayerTransport.HUMAN,
        character_summary="В старом брифинге Астар указан под земным именем Павел Круль.",
    )
    scene = make_scene(
        campaign,
        name="Гданьск - Кафе",
        mode=TurnMode.MANUAL,
        participants=[human],
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content=(
            "Баальтаз открывает старый брифинг и ищет домашний адрес "
            "Астара / Павла Круля."
        ),
    )
    config = GameMasterConfig.objects.create(campaign=campaign, enabled=True)

    context = build_gm_context(scene=scene, config=config)
    request = gm_engine.gm_execution_request(scene)

    assert "Павел Круль" in context.system_prompt
    assert "AUTHORITATIVE-SOURCE GUARD" in request
    assert "AUTHORITATIVE KNOWLEDGE RETRIEVAL" in request
    assert "RETRIEVAL STATUS: NO RELEVANT AUTHORITATIVE RECORDS FOUND" in request
    assert "UNKNOWN/UNAVAILABLE" in request
    assert "never invent missing pre-existing content" in request

    with pytest.raises(ValidationError, match="unsupported exact datum"):
        gm_engine.parse_gm_response(
            json.dumps(
                {
                    "action": "NARRATE",
                    "public": "В старом брифинге указан адрес ul. Szeroka 99.",
                    "private": [],
                    "turn_targets": [],
                },
                ensure_ascii=False,
            ),
            scene=scene,
        )


@pytest.mark.django_db
def test_existing_source_lookup_retrieves_matching_lore_exact_fact():
    campaign = make_campaign()
    human = make_player(
        campaign,
        "Баальтаз",
        transport=PlayerTransport.HUMAN,
    )
    scene = make_scene(
        campaign,
        name="Гданьск - Кафе",
        mode=TurnMode.MANUAL,
        participants=[human],
    )
    LoreEntry.objects.create(
        campaign=campaign,
        title="Астар / Павел Круль",
        category="Target dossier",
        content="Домашний адрес Павла Круля: ul. Długa 17, Gdańsk.",
        enabled=True,
        priority=100,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content="Открывает брифинг и ищет адрес Астара / Павла Круля.",
    )

    request = gm_engine.gm_execution_request(scene)

    assert "RETRIEVAL STATUS: RELEVANT AUTHORITATIVE RECORDS FOUND" in request
    assert "Lore: Астар / Павел Круль" in request
    assert "ul. Długa 17, Gdańsk" in request
    assert "If the requested exact datum is not explicitly supported" in request

    parsed = gm_engine.parse_gm_response(
        json.dumps(
            {
                "action": "NARRATE",
                "public": "В брифинге действительно указан адрес: ul. Długa 17.",
                "private": [],
                "turn_targets": [],
            },
            ensure_ascii=False,
        ),
        scene=scene,
    )
    assert parsed.public.endswith("ul. Długa 17.")


@pytest.mark.django_db
def test_existing_source_retrieval_does_not_stick_after_gm_has_answered():
    campaign = make_campaign()
    human = make_player(campaign, "Баальтаз", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content="Открывает досье и проверяет адрес Нехеша.",
    )

    assert "AUTHORITATIVE KNOWLEDGE RETRIEVAL" in gm_engine.gm_execution_request(scene)

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC,
        content="В доступном досье адрес не указан.",
    )

    assert "AUTHORITATIVE KNOWLEDGE RETRIEVAL" not in gm_engine.gm_execution_request(scene)


@pytest.mark.django_db
def test_gm_prompt_requires_causal_support_for_plot_significant_npc_actions():
    campaign = make_campaign()
    human = make_player(campaign, "Human", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    config = GameMasterConfig.objects.create(campaign=campaign, enabled=True)

    context = build_gm_context(scene=scene, config=config)
    request = gm_engine.gm_execution_request(scene)

    assert "# NPC CAUSALITY" in context.system_prompt
    assert "must not perform suspicious, dramatic, or plot-significant actions merely because" in context.system_prompt
    assert "must NOT make WAIT more common" in context.system_prompt
    assert "Do NOT use this constraint as a reason to prefer WAIT" in request


@pytest.mark.django_db
def test_gm_execution_request_names_active_round_player():
    campaign = make_campaign()
    ned = make_player(campaign, "Нед", transport=PlayerTransport.HUMAN)
    victoria = make_player(campaign, "Виктория")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[ned, victoria],
    )
    scene.round_order = [ned.pk, victoria.pk]
    scene.active_player_index = 0
    scene.save(update_fields=["round_order", "active_player_index", "updated_at"])

    request = gm_engine.gm_execution_request(scene)

    assert "ROUND CONTROL" in request
    assert f"Нед (player_id={ned.pk})" in request
    assert "Do not make an inactive participant the sole required responder" in request
    assert "turn_targets=[]" in request


@pytest.mark.django_db
def test_gm_context_is_omniscient_but_marks_visibility():
    campaign = make_campaign(shared_memory="Общий факт.")
    a = make_player(
        campaign,
        "A",
        character_prompt="Скрытая природа A.",
        memory_summary="Секретная память A.",
    )
    b = make_player(campaign, "B")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[a, b])
    angel = CharacterAppearance.objects.create(
        player=a,
        name="Истинный",
        description="Белые крылья.",
        is_primary=True,
    )
    participation = scene.scene_participants.get(player=a)
    participation.current_appearance = angel
    participation.save(update_fields=["current_appearance"])

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=a,
        content="Публичная заявка.",
        visibility=Visibility.PUBLIC,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=a,
        content="Приватный секрет.",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=a,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="Скрытая заметка мастера.",
        visibility=Visibility.GM_ONLY,
    )
    config = GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        system_prompt="Веди игру аккуратно.",
    )

    context = build_gm_context(scene=scene, config=config)
    history = "\n".join(item["content"] for item in context.messages)

    assert "Скрытая природа A." in context.system_prompt
    assert "Секретная память A." in context.system_prompt
    assert "Белые крылья." in context.system_prompt
    assert "Приватный секрет." in history
    assert "PRIVATE GM↔A" in history
    assert "Скрытая заметка мастера." in history
    assert "GM_ONLY" in history


@pytest.mark.django_db
def test_api_gm_creates_review_draft_then_turn_with_private_context():
    campaign = make_campaign()
    model = make_model("GM model", gateway_model="gm-model")
    human = make_player(
        campaign,
        "Живой",
        transport=PlayerTransport.HUMAN,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.LITELLM,
        model_config=model,
        review_before_publish=True,
    )
    client = StaticGMClient(
        {
            "action": "TURN",
            "public": "Дверь сама открывается.",
            "private": [
                {
                    "player_id": human.pk,
                    "content": "Ты чувствуешь запах озона.",
                }
            ],
            "turn_targets": [human.pk],
        }
    )

    with patch("rpg.services.gm_engine.get_llm_client", return_value=client):
        execution = gm_engine.start_gm_execution(scene=scene)

    assert execution.state == GameMasterExecutionState.DRAFT
    assert execution.action == GameMasterAction.TURN
    assert not Message.objects.filter(scene=scene).exists()
    assert client.calls[0]["model"] == "gm-model"
    assert client.calls[0]["messages"][-1]["role"] == "user"
    assert "## EXECUTION REQUEST" in client.calls[0]["messages"][-1]["content"]
    assert "does not by itself justify WAIT" in client.calls[0]["messages"][-1]["content"]
    assert f"player_id={human.pk}" in client.calls[0]["messages"][-1]["content"]
    assert f"turn_targets=[{human.pk}]" in client.calls[0]["messages"][-1]["content"]

    gm_engine.publish_gm_execution(execution=execution)
    execution.refresh_from_db()

    assert execution.state == GameMasterExecutionState.PUBLISHED
    assert execution.published_turn_id is not None
    turn = execution.published_turn
    public = Message.objects.get(
        turn=turn,
        author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC,
    )
    assert public.content == "Дверь сама открывается."
    private = Message.objects.get(
        turn=turn,
        author_type=AuthorType.GM,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=human,
    )
    assert private.content == "Ты чувствуешь запах озона."

    player_execution = turn.executions.get(player=human)
    assert player_execution.state == ExecutionState.WAITING_HUMAN
    assert private.pk in player_execution.history_message_ids


@pytest.mark.django_db
def test_model_gm_narrate_becomes_turn_for_single_human_in_manual_scene():
    campaign = make_campaign()
    model = make_model("Solo GM", gateway_model="solo-gm")
    human = make_player(
        campaign,
        "Human",
        transport=PlayerTransport.HUMAN,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.LITELLM,
        model_config=model,
        review_before_publish=False,
    )
    client = StaticGMClient(
        {
            "action": "NARRATE",
            "public": "В кафе входит незнакомец и направляется к твоему столику.",
            "private": [],
            "turn_targets": [],
        }
    )

    with patch("rpg.services.gm_engine.get_llm_client", return_value=client):
        execution = gm_engine.start_gm_execution(scene=scene)

    execution.refresh_from_db()
    assert execution.state == GameMasterExecutionState.PUBLISHED
    assert execution.action == GameMasterAction.TURN
    assert execution.action != GameMasterAction.WAIT
    assert execution.turn_targets == [human.pk]
    assert execution.published_turn_id is not None
    player_execution = execution.published_turn.executions.get(player=human)
    assert player_execution.state == ExecutionState.WAITING_HUMAN


@pytest.mark.django_db
def test_model_gm_scene_transition_updates_live_scene_state_and_keeps_turn_open():
    campaign = make_campaign()
    model = make_model("Transition GM", gateway_model="transition-gm")
    human = make_player(
        campaign,
        "Баальтаз",
        transport=PlayerTransport.HUMAN,
    )
    scene = make_scene(
        campaign,
        name="Гданьск - Кафе",
        mode=TurnMode.MANUAL,
        participants=[human],
        description="Баальтаз сидит в кафе у окна.",
        memory_summary="Текущая сцена происходит внутри кафе.",
    )
    GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.LITELLM,
        model_config=model,
        review_before_publish=False,
    )
    transition = {
        "name": "Гданьск - Машина у кафе",
        "description": (
            "Баальтаз находится в припаркованной машине у кафе; двери закрыты, "
            "уличный шум приглушён."
        ),
        "memory": (
            "Баальтаз покинул кафе и сел в машину у здания. "
            "Разговор в кафе остаётся в истории, но текущее место действия — автомобиль."
        ),
    }
    client = StaticGMClient(
        {
            "action": "TURN",
            "public": "Баальтаз закрывает дверь автомобиля. Салон отсекает шум улицы.",
            "private": [],
            "turn_targets": [human.pk],
            "scene_transition": transition,
        }
    )

    with patch("rpg.services.gm_engine.get_llm_client", return_value=client):
        execution = gm_engine.start_gm_execution(scene=scene)

    execution.refresh_from_db()
    scene.refresh_from_db()

    assert execution.state == GameMasterExecutionState.PUBLISHED
    assert execution.scene_transition == transition
    assert scene.name == transition["name"]
    assert scene.description == transition["description"]
    assert scene.memory_summary == transition["memory"]
    assert execution.action == GameMasterAction.TURN
    assert execution.published_turn_id is not None
    player_execution = execution.published_turn.executions.get(player=human)
    assert player_execution.state == ExecutionState.WAITING_HUMAN

    config = GameMasterConfig.objects.get(campaign=campaign)
    refreshed_context = build_gm_context(scene=scene, config=config)
    assert "Scene: Гданьск - Машина у кафе" in refreshed_context.system_prompt
    assert transition["description"] in refreshed_context.system_prompt
    assert transition["memory"] in refreshed_context.system_prompt
    assert "Баальтаз сидит в кафе у окна." not in refreshed_context.system_prompt


@pytest.mark.django_db
def test_wait_cannot_hide_a_scene_transition():
    campaign = make_campaign()
    human = make_player(campaign, "Human", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    with pytest.raises(ValidationError, match="scene_transition"):
        gm_engine.parse_gm_response(
            json.dumps(
                {
                    "action": "WAIT",
                    "public": "",
                    "private": [],
                    "turn_targets": [],
                    "scene_transition": {
                        "name": "Elsewhere",
                        "description": "The current situation is elsewhere.",
                        "memory": "The scene moved elsewhere.",
                    },
                }
            ),
            scene=scene,
        )


@pytest.mark.django_db
def test_gm_narrate_publishes_without_opening_player_turn():
    campaign = make_campaign()
    player = make_player(campaign, "P")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    config = GameMasterConfig.objects.create(campaign=campaign, enabled=True)
    execution = scene.gm_executions.create(
        config=config,
        state=GameMasterExecutionState.DRAFT,
        transport=GameMasterTransport.LITELLM,
        action=GameMasterAction.NARRATE,
        public_draft="Свет в коридоре гаснет.",
        private_drafts=[],
        turn_targets=[],
    )

    gm_engine.publish_gm_execution(execution=execution)
    execution.refresh_from_db()

    assert execution.state == GameMasterExecutionState.PUBLISHED
    assert execution.published_turn_id is None
    assert Message.objects.filter(
        scene=scene,
        author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC,
        content="Свет в коридоре гаснет.",
    ).exists()
    assert not scene.turns.exists()


@pytest.mark.django_db
def test_manual_chat_gm_builds_bridge_and_imports_draft():
    campaign = make_campaign()
    player = make_player(
        campaign,
        "P",
        transport=PlayerTransport.HUMAN,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    config = GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.MANUAL_CHAT,
        manual_chat_label="ChatGPT GM",
        manual_chat_url="https://example.test/gm",
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
        review_before_publish=True,
    )

    execution = gm_engine.start_gm_execution(scene=scene)
    assert execution.state == GameMasterExecutionState.WAITING_EXTERNAL
    assert "MRAZ GAME MASTER CHAT BRIDGE" in execution.external_prompt
    assert "## EXECUTION REQUEST" in execution.external_prompt
    assert "explicitly invoked to produce the next immediate GM beat now" in execution.external_prompt
    assert "does not by itself justify WAIT" in execution.external_prompt
    assert f"player_id={player.pk}" in execution.external_prompt
    assert f"turn_targets=[{player.pk}]" in execution.external_prompt
    assert "TURN|NARRATE|WAIT" in execution.external_prompt
    assert execution.external_is_bootstrap is True

    gm_engine.submit_external_gm_response(
        execution=execution,
        raw_text=json.dumps(
            {
                "action": "NARRATE",
                "public": "За окном раздаётся выстрел.",
                "private": [],
                "turn_targets": [],
            },
            ensure_ascii=False,
        ),
    )

    execution.refresh_from_db()
    config.refresh_from_db()
    assert execution.state == GameMasterExecutionState.DRAFT
    assert execution.action == GameMasterAction.TURN
    assert execution.public_draft == "За окном раздаётся выстрел."
    assert execution.turn_targets == [player.pk]
    assert execution.external_synced_message_ids == execution.context_message_ids
    assert config.manual_chat_initialized is True


@pytest.mark.django_db
def test_scene_ui_shows_model_gm_controls_and_manual_bridge():
    campaign = make_campaign()
    player = make_player(campaign, "P")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    config = GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.MANUAL_CHAT,
        manual_chat_label="External GM",
        manual_chat_url="https://chatgpt.com/c/gm-example",
    )
    client = Client()

    html = client.get(reverse("scene", kwargs={"scene_id": scene.pk})).content.decode()
    assert "Model GM" in html
    assert "Ask GM model" in html

    execution = gm_engine.start_gm_execution(scene=scene)
    html = client.get(reverse("scene", kwargs={"scene_id": scene.pk})).content.decode()
    assert "WAITING EXTERNAL GM" in html
    assert "Copy prompt" in html
    assert "Send via browser bridge" in html
    assert 'data-mraz-bridge-kind="gm"' in html
    assert f'data-mraz-bridge-execution="{execution.pk}"' in html
    assert "data-mraz-bridge-prompt" in html
    assert "data-mraz-bridge-response-form" in html
    assert reverse(
        "submit_external_model_gm",
        kwargs={"scene_id": scene.pk, "execution_id": execution.pk},
    ) in html


@pytest.mark.django_db
def test_discarding_manual_chat_draft_forces_future_bootstrap():
    campaign = make_campaign()
    player = make_player(campaign, "P")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    config = GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
    )

    execution = gm_engine.start_gm_execution(scene=scene)
    gm_engine.submit_external_gm_response(
        execution=execution,
        raw_text=json.dumps(
            {
                "action": "NARRATE",
                "public": "Черновик.",
                "private": [],
                "turn_targets": [],
            }
        ),
    )
    config.refresh_from_db()
    assert config.manual_chat_initialized is True

    gm_engine.discard_gm_execution(execution=execution)
    config.refresh_from_db()
    assert config.manual_chat_initialized is False



@pytest.mark.django_db
def test_discard_gm_execution_without_config_does_not_join_nullable_relation():
    campaign = make_campaign()
    player = make_player(campaign, "P")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    execution = scene.gm_executions.create(
        config=None,
        state=GameMasterExecutionState.DRAFT,
        transport=GameMasterTransport.MANUAL_CHAT,
        external_context_mode=ManualChatContextMode.CHAT_MEMORY,
    )

    gm_engine.discard_gm_execution(execution=execution)
    execution.refresh_from_db()

    assert execution.state == GameMasterExecutionState.DISCARDED


@pytest.mark.django_db
def test_api_gm_can_auto_publish_when_review_is_disabled():
    campaign = make_campaign()
    model = make_model("Auto GM", gateway_model="auto-gm")
    player = make_player(campaign, "P")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.LITELLM,
        model_config=model,
        review_before_publish=False,
    )
    client = StaticGMClient(
        {
            "action": "NARRATE",
            "public": "Автоматически опубликованный мастерский beat.",
            "private": [],
            "turn_targets": [],
        }
    )

    with patch("rpg.services.gm_engine.get_llm_client", return_value=client):
        execution = gm_engine.start_gm_execution(scene=scene)

    assert execution.state == GameMasterExecutionState.PUBLISHED
    assert execution.published_turn_id is None
    assert Message.objects.filter(
        scene=scene,
        author_type=AuthorType.GM,
        visibility=Visibility.PUBLIC,
        content="Автоматически опубликованный мастерский beat.",
    ).exists()


@pytest.mark.django_db
def test_manual_chat_delta_repeats_focused_authoritative_retrieval():
    campaign = make_campaign()
    player = make_player(campaign, "Баальтаз")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    config = GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
        manual_chat_label="Persistent GM",
        manual_chat_url="https://example.test/gm",
    )

    first = gm_engine.start_gm_execution(scene=scene)
    gm_engine.submit_external_gm_response(
        execution=first,
        raw_text=json.dumps(
            {
                "action": "NARRATE",
                "public": "Баальтаз остаётся у стола.",
                "private": [],
                "turn_targets": [],
            },
            ensure_ascii=False,
        ),
    )
    gm_engine.publish_gm_execution(execution=first)

    LoreEntry.objects.create(
        campaign=campaign,
        title="Нехеш / Мацей Войда",
        category="Target dossier",
        content="В досье указан адрес: ul. Na Zaspę 19, Gdańsk.",
        enabled=True,
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=player,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content="Баальтаз открывает досье Нехеша и проверяет его адрес.",
    )

    second = gm_engine.start_gm_execution(scene=scene)

    assert second.external_is_bootstrap is False
    assert "MRAZ GAME MASTER CHAT BRIDGE · DELTA" in second.external_prompt
    assert "AUTHORITATIVE KNOWLEDGE RETRIEVAL" in second.external_prompt
    assert "Lore: Нехеш / Мацей Войда" in second.external_prompt
    assert "ul. Na Zaspę 19, Gdańsk" in second.external_prompt


@pytest.mark.django_db
def test_manual_gm_chat_does_not_reuse_delta_from_parallel_scene():
    campaign = make_campaign()
    player = make_player(campaign, "P")
    first = make_scene(campaign, name="First", mode=TurnMode.MANUAL, participants=[player])
    parallel = make_scene(
        campaign,
        name="Parallel",
        mode=TurnMode.MANUAL,
        participants=[player],
    )
    config = GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
        manual_chat_label="Persistent GM",
        manual_chat_url="https://example.test/gm",
    )

    first_execution = gm_engine.start_gm_execution(scene=first)
    gm_engine.submit_external_gm_response(
        execution=first_execution,
        raw_text=json.dumps(
            {
                "action": "NARRATE",
                "public": "Первый канонический beat.",
                "private": [],
                "turn_targets": [],
            },
            ensure_ascii=False,
        ),
    )
    gm_engine.publish_gm_execution(execution=first_execution)
    config.refresh_from_db()
    assert config.manual_chat_initialized is True

    parallel_execution = gm_engine.start_gm_execution(scene=parallel)
    parallel_execution.refresh_from_db()

    assert parallel_execution.state == GameMasterExecutionState.WAITING_EXTERNAL
    assert parallel_execution.external_is_bootstrap is True
    assert "MRAZ GAME MASTER CHAT BRIDGE · DELTA" not in parallel_execution.external_prompt
    assert "## SYSTEM PROMPT" in parallel_execution.external_prompt



@pytest.mark.django_db
def test_gm_wait_publishes_nothing_and_never_starts_a_player_turn():
    campaign = make_campaign()
    player = make_player(campaign, "P")
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[player])
    config = GameMasterConfig.objects.create(campaign=campaign, enabled=True)
    execution = scene.gm_executions.create(
        config=config,
        state=GameMasterExecutionState.DRAFT,
        transport=GameMasterTransport.LITELLM,
        action=GameMasterAction.WAIT,
        public_draft="",
        private_drafts=[],
        turn_targets=[],
    )

    gm_engine.publish_gm_execution(execution=execution)
    execution.refresh_from_db()

    assert execution.state == GameMasterExecutionState.PUBLISHED
    assert execution.published_turn_id is None
    assert not Message.objects.filter(scene=scene).exists()
    assert not scene.turns.exists()
    assert scene.gm_executions.count() == 1



def test_mock_backend_emits_model_gm_envelope():
    response = MockLLMClient().generate(
        system_prompt="[GAME MASTER]\n# ROLE\nTest GM",
        messages=[],
        model="mock-echo",
    )
    payload = json.loads(response.raw_text)

    assert payload["action"] == "NARRATE"
    assert payload["private"] == []
    assert payload["turn_targets"] == []



@pytest.mark.django_db
def test_manual_gm_chat_uses_delta_inside_scene_lineage():
    campaign = make_campaign()
    player = make_player(campaign, "P")
    first = make_scene(campaign, name="First", mode=TurnMode.MANUAL, participants=[player])
    config = GameMasterConfig.objects.create(
        campaign=campaign,
        enabled=True,
        transport=GameMasterTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
        manual_chat_label="Persistent GM",
        manual_chat_url="https://example.test/gm",
    )

    first_execution = gm_engine.start_gm_execution(scene=first)
    gm_engine.submit_external_gm_response(
        execution=first_execution,
        raw_text=json.dumps(
            {
                "action": "NARRATE",
                "public": "Первый beat.",
                "private": [],
                "turn_targets": [],
            },
            ensure_ascii=False,
        ),
    )
    first_execution.refresh_from_db()
    assert first_execution.external_synced_message_ids == []
    gm_engine.publish_gm_execution(execution=first_execution)

    followup = make_scene(
        campaign,
        name="Follow-up",
        mode=TurnMode.MANUAL,
        participants=[player],
        predecessors=[first],
    )
    next_execution = gm_engine.start_gm_execution(scene=followup)
    next_execution.refresh_from_db()

    assert next_execution.external_is_bootstrap is False
    assert "MRAZ GAME MASTER CHAT BRIDGE · DELTA" in next_execution.external_prompt
    assert "Первый beat." in next_execution.external_prompt
    assert "## EXECUTION REQUEST" in next_execution.external_prompt
    assert "does not by itself justify WAIT" in next_execution.external_prompt
    assert "AUTHORITATIVE-SOURCE GUARD" in next_execution.external_prompt
    assert "NPC CAUSALITY" in next_execution.external_prompt
    assert "scene_transition" in next_execution.external_prompt


@pytest.mark.django_db
def test_matching_gm_fillable_scope_allows_missing_preexisting_weather_details():
    campaign = make_campaign()
    human = make_player(campaign, "Нед", transport=PlayerTransport.HUMAN)
    scene = make_scene(
        campaign,
        name="Буэнос-Айрес. Перед вылетом",
        mode=TurnMode.MANUAL,
        participants=[human],
        description=(
            "HMATS Halcyon готовится к вылету. "
            "[[GM_FILLABLE]]Метеосводка Halcyon перед вылетом из Буэнос-Айреса: "
            "конкретные значения ветра, давления, облачности и прогноз по маршруту "
            "могут быть установлены мастером при первом обращении.[[/GM_FILLABLE]]"
        ),
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content=(
            "Нед открывает метеосводку Halcyon перед вылетом из Буэнос-Айреса "
            "и проверяет прогноз по маршруту."
        ),
    )

    request = gm_engine.gm_execution_request(scene)

    assert "AUTHORITATIVE KNOWLEDGE RETRIEVAL" in request
    assert "EXPLICIT GM_FILLABLE DELEGATION ACTIVE FOR THIS LOOKUP" in request
    assert "Метеосводка Halcyon" in request

    parsed = gm_engine.parse_gm_response(
        json.dumps(
            {
                "action": "NARRATE",
                "public": (
                    "В принятой перед вылетом сводке стоит дата 27.12.1932; "
                    "ветер у побережья северо-восточный, умеренный."
                ),
                "private": [],
                "turn_targets": [],
            },
            ensure_ascii=False,
        ),
        scene=scene,
    )
    assert "27.12.1932" in parsed.public


@pytest.mark.django_db
def test_gm_fillable_permission_does_not_spill_into_unrelated_lookup():
    campaign = make_campaign()
    human = make_player(campaign, "Баальтаз", transport=PlayerTransport.HUMAN)
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[human],
        description=(
            "[[GM_FILLABLE]]Метеосводка Halcyon перед вылетом из Буэнос-Айреса; "
            "погодные значения может определить мастер.[[/GM_FILLABLE]]"
        ),
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content="Баальтаз открывает досье и ищет домашний адрес Астара.",
    )

    request = gm_engine.gm_execution_request(scene)
    assert "EXPLICIT GM_FILLABLE DELEGATION ACTIVE FOR THIS LOOKUP" not in request

    with pytest.raises(ValidationError, match="unsupported exact datum"):
        gm_engine.parse_gm_response(
            json.dumps(
                {
                    "action": "NARRATE",
                    "public": "В досье указан адрес ul. Szeroka 99.",
                    "private": [],
                    "turn_targets": [],
                },
                ensure_ascii=False,
            ),
            scene=scene,
        )


@pytest.mark.django_db
def test_operational_weather_lookup_is_automatically_fillable_without_explicit_tag():
    campaign = make_campaign()
    human = make_player(campaign, "Нед", transport=PlayerTransport.HUMAN)
    scene = make_scene(
        campaign,
        name="Halcyon - в полёте",
        mode=TurnMode.MANUAL,
        participants=[human],
        description="Halcyon следует по плановому маршруту после вылета.",
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content=(
            "Нед открывает текущую метеосводку, сверяет ветер, давление "
            "и прогноз на следующий участок маршрута."
        ),
    )

    request = gm_engine.gm_execution_request(scene)

    assert "Detected information action category: USE_OPERATIONAL_DATA" in request
    assert "AUTO OPERATIONAL SOURCE" in request
    assert "AUTOMATIC GM_FILLABLE OPERATIONAL SCOPE ACTIVE" in request

    parsed = gm_engine.parse_gm_response(
        json.dumps(
            {
                "action": "NARRATE",
                "public": (
                    "В текущей сводке от 26.12.1932 указано давление 1008 гПа; "
                    "ветер северо-восточный, около 12 узлов."
                ),
                "private": [],
                "turn_targets": [],
            },
            ensure_ascii=False,
        ),
        scene=scene,
    )
    assert "26.12.1932" in parsed.public
    assert "1008 гПа" in parsed.public


@pytest.mark.django_db
def test_plain_professional_navigation_work_does_not_trigger_source_guard():
    campaign = make_campaign()
    human = make_player(campaign, "Нед", transport=PlayerTransport.HUMAN)
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[human],
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content=(
            "Нед ведёт счисление, контролирует курс и скорость и рассчитывает "
            "очередную поправку на снос."
        ),
    )

    request = gm_engine.gm_execution_request(scene)

    assert "AUTHORITATIVE KNOWLEDGE RETRIEVAL" not in request


@pytest.mark.django_db
def test_gm_context_marks_player_world_claims_as_declarations_not_confirmed_results():
    campaign = make_campaign()
    human = make_player(campaign, "Нед", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content="Мы ушли с курса ровно на два процента.",
    )
    config = GameMasterConfig.objects.create(campaign=campaign, enabled=True)

    context = build_gm_context(scene=scene, config=config)
    history = "\n".join(item["content"] for item in context.messages)

    assert "# PLAYER DECLARATIONS VS OBJECTIVE WORLD STATE" in context.system_prompt
    assert "PLAYER_ASSERTED_WORLD_STATE" in context.system_prompt
    assert "PLAYER DECLARATION; external-world claims are not objective GM confirmation" in history


@pytest.mark.django_db
def test_gm_context_uses_human_player_as_control_label_not_species_transport():
    campaign = make_campaign()
    human = make_player(campaign, "Нед", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    config = GameMasterConfig.objects.create(campaign=campaign, enabled=True)

    context = build_gm_context(scene=scene, config=config)

    assert "Control: HUMAN_PLAYER" in context.system_prompt
    assert "Transport: HUMAN" not in context.system_prompt


@pytest.mark.django_db
def test_gm_context_contains_routine_pacing_and_professional_guidance():
    campaign = make_campaign()
    human = make_player(campaign, "Нед", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    config = GameMasterConfig.objects.create(campaign=campaign, enabled=True)

    context = build_gm_context(scene=scene, config=config)

    assert "# PACING AND MEANINGFUL CHANGE" in context.system_prompt
    assert "Do not play every minute of stable repetitive work" in context.system_prompt
    assert "# NPC CONVERSATION ENDING" in context.system_prompt
    assert "# PROFESSIONAL COMPETENCE AND TECHNICAL WORK" in context.system_prompt
    assert "# PARALLEL CHARACTER LINES" in context.system_prompt


@pytest.mark.django_db
def test_player_asserted_exact_world_fact_does_not_satisfy_fixed_source_guard():
    campaign = make_campaign()
    human = make_player(campaign, "Баальтаз", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content="Баальтаз утверждает, что домашний адрес Астара — ul. Szeroka 99.",
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content="Баальтаз открывает старое досье и проверяет домашний адрес Астара.",
    )

    request = gm_engine.gm_execution_request(scene)
    assert "READ_EXISTING_SOURCE" in request
    assert "Player declaration history (not objective GM confirmation)" in request

    with pytest.raises(ValidationError, match="unsupported exact datum"):
        gm_engine.parse_gm_response(
            json.dumps(
                {
                    "action": "NARRATE",
                    "public": "В досье действительно указан адрес ul. Szeroka 99.",
                    "private": [],
                    "turn_targets": [],
                },
                ensure_ascii=False,
            ),
            scene=scene,
        )


@pytest.mark.django_db
def test_automatic_operational_fillable_does_not_authorize_unrelated_exact_address():
    campaign = make_campaign()
    human = make_player(campaign, "Нед", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=human,
        visibility=Visibility.PUBLIC,
        action_type="ACT",
        content="Нед открывает текущую метеосводку и проверяет прогноз по маршруту.",
    )

    with pytest.raises(ValidationError, match="unsupported exact datum"):
        gm_engine.parse_gm_response(
            json.dumps(
                {
                    "action": "NARRATE",
                    "public": (
                        "Сводка обещает умеренный ветер. Заодно в ней почему-то "
                        "указан домашний адрес диспетчера: ul. Szeroka 99."
                    ),
                    "private": [],
                    "turn_targets": [],
                },
                ensure_ascii=False,
            ),
            scene=scene,
        )


@pytest.mark.django_db
def test_soft_round_gm_turn_targets_only_meaningful_parallel_line():
    campaign = make_campaign()
    ned = make_player(campaign, "Нед")
    victoria = make_player(campaign, "Виктория")
    scene = make_scene(
        campaign,
        mode=TurnMode.SOFT_ROUND,
        participants=[ned, victoria],
    )

    parsed = gm_engine.parse_gm_response(
        json.dumps(
            {
                "action": "TURN",
                "public": "В каюте Виктории раздаётся стук в дверь.",
                "private": [],
                "turn_targets": [victoria.pk],
                "scene_transition": None,
            },
            ensure_ascii=False,
        ),
        scene=scene,
    )

    assert parsed.turn_targets == [victoria.pk]

    with pytest.raises(ValidationError, match="SOFT_ROUND"):
        gm_engine.parse_gm_response(
            json.dumps(
                {
                    "action": "TURN",
                    "public": "Происходит локальный beat.",
                    "private": [],
                    "turn_targets": [],
                    "scene_transition": None,
                },
                ensure_ascii=False,
            ),
            scene=scene,
        )

    config = GameMasterConfig.objects.create(campaign=campaign, enabled=True)
    context = build_gm_context(scene=scene, config=config)
    assert "SOFT_ROUND is for parallel or loosely coupled character lines" in context.system_prompt
    assert "There is no obligation to alternate mechanically" in context.system_prompt


@pytest.mark.django_db
def test_scene_transition_requires_new_description_and_memory():
    campaign = make_campaign()
    human = make_player(campaign, "P", transport=PlayerTransport.HUMAN)
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[human])

    with pytest.raises(ValidationError, match="description"):
        gm_engine.parse_gm_response(
            json.dumps(
                {
                    "action": "TURN",
                    "public": "Сцена меняется.",
                    "private": [],
                    "turn_targets": [human.pk],
                    "scene_transition": {"name": "Новая сцена"},
                },
                ensure_ascii=False,
            ),
            scene=scene,
        )
