"""Scene/session history, participant isolation, and close/follow-up tests."""
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from rpg.models import AuthorType, Message, Scene, SceneParticipant, TurnMode, Visibility
from rpg.services import turn_engine
from rpg.services.context_builder import build_player_context
from rpg.services.llm import MockLLMClient
from rpg.tests.factories import make_campaign, make_player, make_scene


def _contents(ctx):
    return "\n".join(message["content"] for message in ctx.messages)



class RecordingClient(MockLLMClient):
    def __init__(self):
        self.calls = []

    def generate(self, *, system_prompt, messages, model, temperature=0.7):
        self.calls.append(model)
        return super().generate(
            system_prompt=system_prompt,
            messages=messages,
            model=model,
            temperature=temperature,
        )


@pytest.fixture
def recording_client(mock_backend):
    client = RecordingClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        yield client



@pytest.mark.django_db
def test_predecessor_history_follows_scene_participation_without_leaking_parallel_branches():
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    mila = make_player(campaign, "Mila")
    mathis = make_player(campaign, "Mathis")

    lucien_solo = make_scene(
        campaign,
        name="Lucien solo",
        mode=TurnMode.MANUAL,
        participants=[lucien],
    )
    Message.objects.create(
        campaign=campaign,
        scene=lucien_solo,
        author_type=AuthorType.GM,
        content="LUCien-only apartment history",
        visibility=Visibility.PUBLIC,
    )
    Scene.objects.filter(pk=lucien_solo.pk).update(is_closed=True)
    lucien_solo.refresh_from_db()

    mila_solo = make_scene(
        campaign,
        name="Mila solo",
        mode=TurnMode.MANUAL,
        participants=[mila],
    )
    Message.objects.create(
        campaign=campaign,
        scene=mila_solo,
        author_type=AuthorType.GM,
        content="MILA-only park history",
        visibility=Visibility.PUBLIC,
    )
    Scene.objects.filter(pk=mila_solo.pk).update(is_closed=True)
    mila_solo.refresh_from_db()

    duo = make_scene(
        campaign,
        name="Lucien and Mathis",
        mode=TurnMode.MANUAL,
        participants=[lucien, mathis],
        predecessors=[lucien_solo],
    )
    Message.objects.create(
        campaign=campaign,
        scene=duo,
        author_type=AuthorType.GM,
        content="DUO shared cafe history",
        visibility=Visibility.PUBLIC,
    )
    Scene.objects.filter(pk=duo.pk).update(is_closed=True)
    duo.refresh_from_db()

    trio = make_scene(
        campaign,
        name="Canal trio",
        mode=TurnMode.MANUAL,
        participants=[lucien, mila, mathis],
        predecessors=[duo, mila_solo],
    )

    lucien_ctx = build_player_context(player=lucien, scene=trio)
    mila_ctx = build_player_context(player=mila, scene=trio)
    mathis_ctx = build_player_context(player=mathis, scene=trio)

    lucien_text = _contents(lucien_ctx)
    mila_text = _contents(mila_ctx)
    mathis_text = _contents(mathis_ctx)

    assert "LUCien-only apartment history" in lucien_text
    assert "DUO shared cafe history" in lucien_text
    assert "MILA-only park history" not in lucien_text

    assert "MILA-only park history" in mila_text
    assert "LUCien-only apartment history" not in mila_text
    assert "DUO shared cafe history" not in mila_text

    assert "DUO shared cafe history" in mathis_text
    assert "LUCien-only apartment history" not in mathis_text
    assert "MILA-only park history" not in mathis_text


@pytest.mark.django_db
def test_private_predecessor_history_stays_private_after_threads_merge():
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    mila = make_player(campaign, "Mila")

    solo = make_scene(
        campaign,
        name="Lucien private thread",
        mode=TurnMode.MANUAL,
        participants=[lucien],
    )
    Message.objects.create(
        campaign=campaign,
        scene=solo,
        author_type=AuthorType.GM,
        content="SECRET-KING",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=lucien,
    )
    Message.objects.create(
        campaign=campaign,
        scene=solo,
        author_type=AuthorType.GM,
        content="GM-ONLY-NOTE",
        visibility=Visibility.GM_ONLY,
    )
    Scene.objects.filter(pk=solo.pk).update(is_closed=True)
    solo.refresh_from_db()

    shared = make_scene(
        campaign,
        name="Shared",
        mode=TurnMode.MANUAL,
        participants=[lucien, mila],
        predecessors=[solo],
    )

    lucien_ctx = build_player_context(player=lucien, scene=shared)
    mila_ctx = build_player_context(player=mila, scene=shared)

    assert "SECRET-KING" in _contents(lucien_ctx)
    assert "SECRET-KING" not in _contents(mila_ctx)
    assert "GM-ONLY-NOTE" not in _contents(lucien_ctx)
    assert "GM-ONLY-NOTE" not in _contents(mila_ctx)


@pytest.mark.django_db
def test_predecessor_scene_summary_is_inherited_only_by_its_participants():
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    mila = make_player(campaign, "Mila")

    solo = make_scene(
        campaign,
        name="Apartment",
        mode=TurnMode.MANUAL,
        participants=[lucien],
        memory_summary="Lucien learned a private fact in the apartment.",
        is_closed=True,
    )
    current = make_scene(
        campaign,
        name="Canal",
        mode=TurnMode.MANUAL,
        participants=[lucien, mila],
        predecessors=[solo],
    )

    lucien_ctx = build_player_context(player=lucien, scene=current)
    mila_ctx = build_player_context(player=mila, scene=current)

    assert "Lucien learned a private fact in the apartment." in lucien_ctx.system_prompt
    assert "Lucien learned a private fact in the apartment." not in mila_ctx.system_prompt


@pytest.mark.django_db
def test_nonparticipant_cannot_be_selected_for_turn(recording_client):
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    mila = make_player(campaign, "Mila")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[lucien],
    )

    with pytest.raises(ValidationError, match="not a participant"):
        turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[mila],
        )

    assert recording_client.calls == []


@pytest.mark.django_db
def test_round_order_is_scoped_to_scene_participants(mock_backend):
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    mila = make_player(campaign, "Mila")
    make_player(campaign, "Mathis")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[lucien, mila],
    )

    response = Client().post(
        reverse("set_mode", kwargs={"scene_id": scene.pk}),
        {
            "mode": TurnMode.ROUND,
            "round_order": [str(lucien.pk), str(mila.pk)],
        },
    )

    assert response.status_code == 302
    scene.refresh_from_db()
    assert scene.round_order == [lucien.pk, mila.pk]


@pytest.mark.django_db
def test_close_scene_makes_public_private_and_mode_writes_read_only(mock_backend):
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[lucien],
    )
    client = Client()

    response = client.post(reverse("close_scene", kwargs={"scene_id": scene.pk}))
    assert response.status_code == 302

    scene.refresh_from_db()
    assert scene.is_closed is True
    assert scene.closed_at is not None

    public = client.post(
        reverse("send_gm_message", kwargs={"scene_id": scene.pk}),
        {"content": "too late", "run_turn": "0"},
    )
    private = client.post(
        reverse(
            "send_private_message",
            kwargs={"scene_id": scene.pk, "player_id": lucien.pk},
        ),
        {"content": "also too late"},
    )
    mode = client.post(
        reverse("set_mode", kwargs={"scene_id": scene.pk}),
        {"mode": TurnMode.MANUAL},
    )

    assert public.status_code == 400
    assert private.status_code == 400
    assert mode.status_code == 400
    assert not Message.objects.filter(scene=scene, content__contains="too late").exists()

    page = client.get(reverse("scene", kwargs={"scene_id": scene.pk}))
    html = page.content.decode()
    assert "CLOSED" in html
    assert "New scene from here" in html
    assert 'id="composer"' not in html


@pytest.mark.django_db
def test_message_model_rejects_direct_write_to_closed_scene():
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[lucien],
        is_closed=True,
    )

    with pytest.raises(ValidationError, match="closed scene"):
        Message.objects.create(
            campaign=campaign,
            scene=scene,
            author_type=AuthorType.GM,
            content="forbidden",
            visibility=Visibility.PUBLIC,
        )


@pytest.mark.django_db
def test_turn_engine_refuses_closed_scene(recording_client):
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[lucien],
        is_closed=True,
    )

    with pytest.raises(ValidationError, match="closed"):
        turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )

    assert recording_client.calls == []


@pytest.mark.django_db
def test_followup_scene_inherits_closed_source_and_selected_participants():
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    mila = make_player(campaign, "Mila")
    mathis = make_player(campaign, "Mathis")
    source = make_scene(
        campaign,
        name="Episode 1",
        mode=TurnMode.MANUAL,
        participants=[lucien, mathis],
        is_closed=True,
    )

    response = Client().post(
        reverse("create_followup_scene", kwargs={"scene_id": source.pk}),
        {
            "name": "Episode 2",
            "participants": [str(lucien.pk), str(mila.pk), str(mathis.pk)],
        },
    )

    assert response.status_code == 302
    created = Scene.objects.get(campaign=campaign, name="Episode 2")
    assert created.mode == TurnMode.MANUAL
    assert created.is_closed is False
    assert list(created.previous_scenes.all()) == [source]
    assert list(
        created.scene_participants.order_by("order", "pk")
        .values_list("player_id", flat=True)
    ) == [lucien.pk, mila.pk, mathis.pk]


@pytest.mark.django_db
def test_followup_requires_closed_source():
    campaign = make_campaign()
    lucien = make_player(campaign, "Lucien")
    source = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[lucien],
    )

    response = Client().post(
        reverse("create_followup_scene", kwargs={"scene_id": source.pk}),
        {"name": "Should not exist", "participants": [str(lucien.pk)]},
    )

    assert response.status_code == 400
    assert not Scene.objects.filter(name="Should not exist").exists()
