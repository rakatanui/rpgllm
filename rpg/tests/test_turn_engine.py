"""Turn Engine regression tests for orchestration semantics."""
import uuid
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from rpg.models import (
    AuthorType,
    ExecutionState,
    Message,
    PlayerStatus,
    Turn,
    TurnMode,
    TurnState,
    Visibility,
)
from rpg.services import turn_engine
from rpg.services.llm import LLMResponse, MockLLMClient
from rpg.tests.factories import make_campaign, make_player, make_scene, make_three_players


def _resp(name, action="ACT", public=None, private=""):
    return LLMResponse(
        raw_text="{}",
        action_type=action,
        public=public if public is not None else f"[{action}] {name}",
        private_to_gm=private,
    )


class RecordingClient(MockLLMClient):
    def __init__(self):
        self.calls = []
        self.histories = {}
        self.prompts = {}

    def generate(self, *, system_prompt, messages, model, temperature=0.7):
        name = "Unknown"
        for line in system_prompt.splitlines():
            if line.startswith("[PLAYER:"):
                name = line[len("[PLAYER:"):].rstrip("]").strip()
                break
        self.calls.append(name)
        self.histories.setdefault(name, []).append(list(messages))
        self.prompts[name] = system_prompt
        action = "PASS" if "You are NOT the active player this round." in system_prompt else "ACT"
        return _resp(name, action=action)


@pytest.fixture
def recording_client(mock_backend):
    client = RecordingClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        yield client


@pytest.mark.django_db
def test_manual_calls_only_selected_players(recording_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.MANUAL)
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="hello",
        selected_players=[mila],
    )
    assert recording_client.calls == ["Mila"]
    assert result.turn.participants == [mila.pk]


@pytest.mark.django_db
def test_round_uses_frozen_snapshot_and_advances_once(recording_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk, mathis.pk],
        active_player_index=0,
    )

    turn_engine.start_turn(scene=scene, gm_message_text="round")

    assert recording_client.calls == ["Lucien", "Mila", "Mathis"]
    for name in ("Lucien", "Mila", "Mathis"):
        contents = " ".join(item["content"] for item in recording_client.histories[name][0])
        for other in ("Lucien", "Mila", "Mathis"):
            if other != name:
                assert f"[ACT] {other}" not in contents
                assert f"[PASS] {other}" not in contents

    scene.refresh_from_db()
    assert scene.active_player_index == 1


@pytest.mark.django_db
def test_private_message_in_round_calls_only_target_and_does_not_advance(recording_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk, mathis.pk],
        active_player_index=0,
    )

    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="secret",
        selected_players=[mila],
        private_to_player=mila,
    )

    assert recording_client.calls == ["Mila"]
    assert result.turn.is_private is True
    scene.refresh_from_db()
    assert scene.active_player_index == 0

    public = Message.objects.filter(turn=result.turn, visibility=Visibility.PUBLIC)
    assert not public.exists()
    private = Message.objects.filter(
        turn=result.turn,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=mila,
    )
    assert private.count() >= 2

    lucien_context = __import__(
        "rpg.services.context_builder", fromlist=["build_player_context"]
    ).build_player_context(player=lucien, scene=scene)
    lucien_text = " ".join(m["content"] for m in lucien_context.messages)
    assert "secret" not in lucien_text


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("active", "action", "valid"),
    [
        (True, "ACT", True),
        (True, "PASS", True),
        (True, "ACT_OUT_OF_TURN", False),
        (False, "PASS", True),
        (False, "ACT_OUT_OF_TURN", True),
        (False, "ACT", False),
    ],
)
def test_round_server_validates_action_types(mock_backend, active, action, valid):
    camp = make_campaign()
    lucien, mila, _ = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
        participants=[lucien, mila],
    )

    class ActionClient(MockLLMClient):
        def generate(self, *, system_prompt, messages, model, temperature=0.7):
            is_inactive = "You are NOT the active player this round." in system_prompt
            if (not active) == is_inactive:
                return _resp("target", action=action)
            return _resp("other", action="PASS")

    with patch("rpg.services.turn_engine.get_llm_client", return_value=ActionClient()):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")

    target_player = lucien if active else mila
    execution = result.turn.executions.get(player=target_player)
    if valid:
        assert execution.state == ExecutionState.COMPLETED
    else:
        assert execution.state == ExecutionState.INVALID
        assert not Message.objects.filter(
            turn=result.turn,
            execution=execution,
            author_type=AuthorType.PLAYER,
        ).exists()


@pytest.mark.django_db
def test_private_to_gm_not_stored_on_public_message(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL)

    class PrivateClient(MockLLMClient):
        def generate(self, **kwargs):
            return _resp("Lucien", private="hidden detail")

    with patch("rpg.services.turn_engine.get_llm_client", return_value=PrivateClient()):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )

    public = Message.objects.get(
        turn=result.turn,
        author_type=AuthorType.PLAYER,
        visibility=Visibility.PUBLIC,
    )
    assert "hidden detail" not in public.content
    assert not hasattr(public, "private_to_gm")
    private = Message.objects.get(
        turn=result.turn,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        content="hidden detail",
    )
    assert private.gm_unread is True


@pytest.mark.django_db
def test_private_to_gm_exact_public_duplicate_is_not_stored(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL)

    class DuplicatePrivateClient(MockLLMClient):
        def generate(self, **kwargs):
            return LLMResponse(
                raw_text="{}",
                action_type="ACT",
                public="I inspect the lock.",
                private_to_gm="I inspect the lock.",
            )

    with patch(
        "rpg.services.turn_engine.get_llm_client",
        return_value=DuplicatePrivateClient(),
    ):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )

    assert Message.objects.filter(
        turn=result.turn,
        visibility=Visibility.PUBLIC,
        content="I inspect the lock.",
    ).count() == 1
    assert not Message.objects.filter(
        turn=result.turn,
        visibility=Visibility.PRIVATE_GM_PLAYER,
    ).exists()


@pytest.mark.django_db
def test_private_turn_player_reply_is_marked_unread(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL)

    class PrivateReplyClient(MockLLMClient):
        def generate(self, **kwargs):
            return LLMResponse(
                raw_text="{}",
                action_type="ACT",
                public="Я отвечаю мастеру.",
                private_to_gm="",
            )

    with patch(
        "rpg.services.turn_engine.get_llm_client",
        return_value=PrivateReplyClient(),
    ):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="Что ты делаешь?",
            selected_players=[lucien],
            private_to_player=lucien,
        )

    reply = Message.objects.get(
        turn=result.turn,
        author_type=AuthorType.PLAYER,
        visibility=Visibility.PRIVATE_GM_PLAYER,
    )
    assert reply.gm_unread is True


@pytest.mark.django_db
def test_retry_failed_execution_does_not_duplicate_successes(mock_backend):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.TABLE, round_order=[lucien.pk, mila.pk, mathis.pk])

    class FlakyClient(MockLLMClient):
        def __init__(self):
            self.mila_fail = True
            self.calls = []

        def generate(self, *, system_prompt, messages, model, temperature=0.7):
            name = next(
                line[len("[PLAYER:"):].rstrip("]").strip()
                for line in system_prompt.splitlines()
                if line.startswith("[PLAYER:")
            )
            self.calls.append(name)
            if name == "Mila" and self.mila_fail:
                raise RuntimeError("provider down")
            return _resp(name)

    client = FlakyClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")
        result.turn.refresh_from_db()
        assert result.turn.state == TurnState.FAILED
        before = Message.objects.filter(
            turn=result.turn,
            author_type=AuthorType.PLAYER,
            visibility=Visibility.PUBLIC,
        ).count()
        client.mila_fail = False
        mila_execution = result.turn.executions.get(player=mila)
        retried = turn_engine.retry_execution(mila_execution)

    after = Message.objects.filter(
        turn=result.turn,
        author_type=AuthorType.PLAYER,
        visibility=Visibility.PUBLIC,
    ).count()
    assert after == before + 1
    assert client.calls.count("Lucien") == 1
    assert client.calls.count("Mathis") == 1
    assert client.calls.count("Mila") == 2
    retried.turn.refresh_from_db()
    assert retried.turn.state == TurnState.COMPLETED


@pytest.mark.django_db
def test_retry_round_does_not_advance_again(mock_backend):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk, mathis.pk],
        active_player_index=0,
    )

    class FlakyInactive(MockLLMClient):
        def __init__(self):
            self.fail_once = True

        def generate(self, *, system_prompt, messages, model, temperature=0.7):
            name = next(
                line[len("[PLAYER:"):].rstrip("]").strip()
                for line in system_prompt.splitlines()
                if line.startswith("[PLAYER:")
            )
            if name == "Mila" and self.fail_once:
                self.fail_once = False
                raise RuntimeError("once")
            action = "PASS" if "NOT the active" in system_prompt else "ACT"
            return _resp(name, action=action)

    client = FlakyInactive()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")
        scene.refresh_from_db()
        assert scene.active_player_index == 0
        turn_engine.retry_execution(result.turn.executions.get(player=mila))

    scene.refresh_from_db()
    assert scene.active_player_index == 1


@pytest.mark.django_db
def test_round_rejects_empty_order(recording_client):
    camp = make_campaign()
    make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.ROUND, round_order=[])

    with pytest.raises(ValidationError, match="confirmed player order"):
        turn_engine.start_turn(scene=scene, gm_message_text="go")

    assert recording_client.calls == []
    assert Turn.objects.count() == 0


@pytest.mark.django_db
def test_failed_round_does_not_advance_until_retry_succeeds(mock_backend):
    camp = make_campaign()
    lucien, mila, _ = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    class FailMilaOnce(MockLLMClient):
        def __init__(self):
            self.failed = False

        def generate(self, *, system_prompt, messages, model, temperature=0.7):
            name = next(
                line[len("[PLAYER:"):].rstrip("]").strip()
                for line in system_prompt.splitlines()
                if line.startswith("[PLAYER:")
            )
            if name == "Mila" and not self.failed:
                self.failed = True
                raise RuntimeError("temporary")
            action = "PASS" if "NOT the active" in system_prompt else "ACT"
            return _resp(name, action=action)

    client = FailMilaOnce()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")
        scene.refresh_from_db()
        assert scene.active_player_index == 0
        assert result.turn.state == TurnState.FAILED

        turn_engine.retry_execution(result.turn.executions.get(player=mila))

    scene.refresh_from_db()
    assert scene.active_player_index == 1


@pytest.mark.django_db
def test_same_client_turn_id_is_idempotent(recording_client):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL)
    key = uuid.uuid4()

    first = turn_engine.start_turn(
        scene=scene,
        gm_message_text="hello",
        selected_players=[lucien],
        client_turn_id=key,
    )
    second = turn_engine.start_turn(
        scene=scene,
        gm_message_text="hello",
        selected_players=[lucien],
        client_turn_id=key,
    )

    assert first.turn.pk == second.turn.pk
    assert Turn.objects.filter(scene=scene, client_turn_id=key).count() == 1
    assert recording_client.calls == ["Lucien"]


@pytest.mark.django_db
def test_cannot_select_foreign_campaign_player(recording_client):
    camp_a = make_campaign("A")
    camp_b = make_campaign("B")
    local = make_player(camp_a, "Local")
    foreign = make_player(camp_b, "Foreign")
    scene = make_scene(camp_a, mode=TurnMode.MANUAL)

    with pytest.raises(ValidationError):
        turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[local, foreign],
        )
    assert recording_client.calls == []


@pytest.mark.django_db
def test_saving_player_message_never_starts_turn(recording_client):
    camp = make_campaign()
    player = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL)
    before = Turn.objects.count()

    Message.objects.create(
        campaign=camp,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=player,
        content="manual AI reply",
        visibility=Visibility.PUBLIC,
    )

    assert Turn.objects.count() == before
    assert recording_client.calls == []


@pytest.mark.django_db
def test_mock_mode_makes_no_http_call(mock_backend):
    camp = make_campaign()
    player = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL)
    with patch("rpg.services.llm.httpx.Client") as mock_http:
        turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[player],
        )
        mock_http.assert_not_called()
