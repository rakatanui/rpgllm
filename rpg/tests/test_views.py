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
