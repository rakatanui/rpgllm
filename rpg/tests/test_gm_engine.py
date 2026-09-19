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
    player = make_player(campaign, "P")
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
    assert execution.public_draft == "За окном раздаётся выстрел."
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
    )
    client = Client()

    html = client.get(reverse("scene", kwargs={"scene_id": scene.pk})).content.decode()
    assert "Model GM" in html
    assert "Ask GM model" in html

    execution = gm_engine.start_gm_execution(scene=scene)
    html = client.get(reverse("scene", kwargs={"scene_id": scene.pk})).content.decode()
    assert "WAITING EXTERNAL GM" in html
    assert "Copy prompt" in html
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
