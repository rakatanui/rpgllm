"""Turn Engine regression tests for orchestration semantics."""
import threading
import uuid
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError

from rpg.models import (
    AuthorType,
    ExecutionState,
    ManualChatContextMode,
    Message,
    PlayerStatus,
    PlayerTransport,
    Turn,
    TurnMode,
    TurnState,
    Visibility,
)
from rpg.services import turn_engine
from rpg.services.llm import LLMResponse, MockLLMClient
from rpg.tests.factories import make_campaign, make_model, make_player, make_scene, make_three_players


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

    assert sorted(recording_client.calls) == ["Lucien", "Mathis", "Mila"]
    for name in ("Lucien", "Mila", "Mathis"):
        contents = " ".join(item["content"] for item in recording_client.histories[name][0])
        for other in ("Lucien", "Mila", "Mathis"):
            if other != name:
                assert f"[ACT] {other}" not in contents
                assert f"[PASS] {other}" not in contents

    scene.refresh_from_db()
    assert scene.active_player_index == 1
    assert "1200 visible characters, 6 paragraphs" in recording_client.prompts["Lucien"]
    assert "prefer 2-3 paragraphs" in recording_client.prompts["Lucien"]


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
        participants=[lucien, mila],
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



@pytest.mark.django_db
def test_public_act_allows_six_paragraphs_and_two_questions(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class AllowedClient(MockLLMClient):
        def generate(self, **kwargs):
            return _resp(
                "Lucien",
                public=(
                    "Первый абзац.\n\n"
                    "Второй абзац.\n\n"
                    "Третий абзац.\n\n"
                    "Четвёртый абзац. Где он?\n\n"
                    "Пятый абзац. Кто его видел?\n\n"
                    "Шестой абзац."
                ),
            )

    with patch("rpg.services.turn_engine.get_llm_client", return_value=AllowedClient()):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )

    execution = result.turn.executions.get(player=lucien)
    assert execution.state == ExecutionState.COMPLETED


@pytest.mark.django_db
def test_public_act_rejects_seven_paragraphs(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class VerboseClient(MockLLMClient):
        def generate(self, **kwargs):
            return _resp(
                "Lucien",
                public=(
                    "Первый.\n\nВторой.\n\nТретий.\n\n"
                    "Четвёртый.\n\nПятый.\n\nШестой.\n\nСедьмой."
                ),
            )

    with patch("rpg.services.turn_engine.get_llm_client", return_value=VerboseClient()):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )

    execution = result.turn.executions.get(player=lucien)
    assert execution.state == ExecutionState.INVALID
    assert "ACT response has 7 paragraphs; maximum is 6" in execution.error


@pytest.mark.django_db
def test_public_act_rejects_three_questions(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class QuestionnaireClient(MockLLMClient):
        def generate(self, **kwargs):
            return _resp(
                "Lucien",
                public="Где он? Кто его видел? Когда это случилось?",
            )

    with patch("rpg.services.turn_engine.get_llm_client", return_value=QuestionnaireClient()):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )

    execution = result.turn.executions.get(player=lucien)
    assert execution.state == ExecutionState.INVALID
    assert "ACT response asks 3 questions; maximum is 2" in execution.error


@pytest.mark.django_db
def test_out_of_turn_keeps_two_paragraph_one_question_limits(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    mila = make_player(camp, "Mila")
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
        participants=[lucien, mila],
    )

    class InterruptClient(MockLLMClient):
        def generate(self, *, system_prompt, **kwargs):
            if "NOT the active" in system_prompt:
                return _resp(
                    "Mila",
                    action="ACT_OUT_OF_TURN",
                    public="Первый вопрос? Второй вопрос?",
                )
            return _resp("Lucien", action="ACT", public="Lucien waits.")

    with patch("rpg.services.turn_engine.get_llm_client", return_value=InterruptClient()):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")

    execution = result.turn.executions.get(player=mila)
    assert execution.state == ExecutionState.INVALID
    assert "ACT_OUT_OF_TURN response asks 2 questions; maximum is 1" in execution.error


@pytest.mark.django_db
def test_out_of_turn_response_has_stricter_length_limit(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    mila = make_player(camp, "Mila")
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
        participants=[lucien, mila],
    )

    class LongInterruptClient(MockLLMClient):
        def generate(self, *, system_prompt, **kwargs):
            if "NOT the active" in system_prompt:
                return _resp("Mila", action="ACT_OUT_OF_TURN", public="x" * 651)
            return _resp("Lucien", action="ACT", public="Lucien waits.")

    with patch("rpg.services.turn_engine.get_llm_client", return_value=LongInterruptClient()):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")

    execution = result.turn.executions.get(player=mila)
    assert execution.state == ExecutionState.INVALID
    assert "ACT_OUT_OF_TURN response is too long" in execution.error


def test_response_budget_ignores_hidden_russian_translation():
    turn = Turn(mode=TurnMode.MANUAL, is_private=False)
    response = LLMResponse(
        raw_text="{}",
        action_type="ACT",
        public=(
            "[[SPEECH]]Bonjour.[[RU]]"
            + ("Очень длинный скрытый перевод. " * 100)
            + "[[/SPEECH]]"
        ),
    )

    turn_engine._validate_response_discipline(
        turn=turn,
        out_of_turn=False,
        response=response,
    )



@pytest.mark.django_db
def test_regenerate_completed_execution_replaces_same_public_message_only(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class RegenClient(MockLLMClient):
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            return _resp(
                "Lucien",
                public="Плохой вариант." if self.calls == 1 else "Новый вариант.",
            )

    client = RegenClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )
        execution = result.turn.executions.get(player=lucien)
        original = Message.objects.get(
            execution=execution,
            author_type=AuthorType.PLAYER,
            visibility=Visibility.PUBLIC,
        )
        original_pk = original.pk

        regenerated = turn_engine.regenerate_execution(execution)

    original.refresh_from_db()
    execution.refresh_from_db()
    assert client.calls == 2
    assert regenerated.messages[0].pk == original_pk
    assert original.content == "Новый вариант."
    assert execution.state == ExecutionState.COMPLETED
    assert Message.objects.filter(
        execution=execution,
        author_type=AuthorType.PLAYER,
        visibility=Visibility.PUBLIC,
    ).count() == 1


@pytest.mark.django_db
def test_regenerate_round_execution_does_not_advance_or_recall_peers(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    mila = make_player(camp, "Mila")
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        participants=[lucien, mila],
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    class CountingClient(MockLLMClient):
        def __init__(self):
            self.calls = []

        def generate(self, *, system_prompt, **kwargs):
            name = next(
                line[len("[PLAYER:"):].rstrip("]").strip()
                for line in system_prompt.splitlines()
                if line.startswith("[PLAYER:")
            )
            self.calls.append(name)
            if "NOT the active" in system_prompt:
                return _resp(name, action="PASS", public="")
            return _resp(name, action="ACT", public=f"{name} acts.")

    client = CountingClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")
        scene.refresh_from_db()
        assert scene.active_player_index == 1

        turn_engine.regenerate_execution(result.turn.executions.get(player=lucien))

    scene.refresh_from_db()
    assert scene.active_player_index == 1
    assert client.calls.count("Lucien") == 2
    assert client.calls.count("Mila") == 1


@pytest.mark.django_db
def test_failed_regeneration_keeps_original_successful_reply(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class InvalidRegenClient(MockLLMClient):
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _resp("Lucien", public="Исходная заявка.")
            return _resp("Lucien", public="x" * 1201)

    client = InvalidRegenClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )
        execution = result.turn.executions.get(player=lucien)
        original = Message.objects.get(
            execution=execution,
            author_type=AuthorType.PLAYER,
            visibility=Visibility.PUBLIC,
        )

        with pytest.raises(RuntimeError, match="Regeneration failed"):
            turn_engine.regenerate_execution(execution)

    original.refresh_from_db()
    execution.refresh_from_db()
    assert original.content == "Исходная заявка."
    assert execution.state == ExecutionState.COMPLETED


@pytest.mark.django_db
def test_ooc_feedback_can_replace_existing_public_declaration(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class OocClient(MockLLMClient):
        def generate(self, *, system_prompt, **kwargs):
            if "# OOC REVISION" in system_prompt:
                return LLMResponse(
                    raw_text="{}",
                    action_type="ACT",
                    public="Люсьен коротко кивает.",
                    private_to_gm="Сократил заявку.",
                )
            return _resp(
                "Lucien",
                action="ACT",
                public="Люсьен произносит длинную и неуместную речь.",
            )

    with patch("rpg.services.turn_engine.get_llm_client", return_value=OocClient()):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )
        execution = result.turn.executions.get(player=lucien)
        public = Message.objects.get(
            execution=execution,
            author_type=AuthorType.PLAYER,
            visibility=Visibility.PUBLIC,
        )
        public_pk = public.pk

        private_reply, changed = turn_engine.revise_execution_ooc(
            public_message=public,
            gm_comment="Без речи. Только короткая реакция.",
        )

    public.refresh_from_db()
    execution.refresh_from_db()
    assert changed is True
    assert public.pk == public_pk
    assert public.content == "Люсьен коротко кивает."
    assert execution.state == ExecutionState.COMPLETED
    assert Message.objects.filter(
        scene=scene,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=lucien,
        author_type=AuthorType.GM,
        content__contains="Без речи. Только короткая реакция.",
    ).exists()
    assert private_reply.visibility == Visibility.PRIVATE_GM_PLAYER
    assert "Заявка изменена" in private_reply.content
    assert "Сократил заявку." in private_reply.content
    assert Turn.objects.filter(scene=scene).count() == 1


@pytest.mark.django_db
def test_ooc_feedback_may_leave_declaration_unchanged(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])
    original_text = "Люсьен остаётся у двери."

    class KeepClient(MockLLMClient):
        def generate(self, *, system_prompt, **kwargs):
            return _resp("Lucien", public=original_text)

    with patch("rpg.services.turn_engine.get_llm_client", return_value=KeepClient()):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )
        public = Message.objects.get(
            execution__turn=result.turn,
            author_type=AuthorType.PLAYER,
            visibility=Visibility.PUBLIC,
        )

        private_reply, changed = turn_engine.revise_execution_ooc(
            public_message=public,
            gm_comment="Ты точно хочешь остаться?",
        )

    public.refresh_from_db()
    assert changed is False
    assert public.content == original_text
    assert "без изменений" in private_reply.content



@pytest.mark.django_db
def test_round_silence_runs_round_without_gm_message_and_advances(recording_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        participants=[lucien, mila, mathis],
        round_order=[lucien.pk, mila.pk, mathis.pk],
        active_player_index=0,
    )

    result = turn_engine.start_silent_turn(scene=scene)

    assert result.turn.trigger_message_id is None
    assert sorted(recording_client.calls) == ["Lucien", "Mathis", "Mila"]
    assert not Message.objects.filter(
        turn=result.turn,
        author_type=AuthorType.GM,
    ).exists()
    assert "# GM SILENCE" in recording_client.prompts["Lucien"]
    assert "yielding the floor to the players" in recording_client.prompts["Lucien"]

    scene.refresh_from_db()
    assert scene.active_player_index == 1


@pytest.mark.django_db
def test_manual_silence_calls_only_selected_player_without_gm_message(recording_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.MANUAL,
        participants=[lucien, mila, mathis],
    )

    result = turn_engine.start_silent_turn(
        scene=scene,
        selected_players=[mila],
    )

    assert recording_client.calls == ["Mila"]
    assert result.turn.participants == [mila.pk]
    assert result.turn.trigger_message_id is None
    assert not Message.objects.filter(
        turn=result.turn,
        author_type=AuthorType.GM,
    ).exists()
    assert "# GM SILENCE" in recording_client.prompts["Mila"]


@pytest.mark.django_db
def test_silence_rejects_unsupported_mode(recording_client):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(
        camp,
        mode=TurnMode.SIMULTANEOUS,
        participants=[lucien],
    )

    with pytest.raises(ValidationError, match="ROUND or MANUAL"):
        turn_engine.start_silent_turn(
            scene=scene,
            selected_players=[lucien],
        )

    assert recording_client.calls == []
    assert Turn.objects.filter(scene=scene).count() == 0



@pytest.mark.django_db
def test_round_provider_calls_are_parallel(mock_backend):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        participants=[lucien, mila, mathis],
        round_order=[lucien.pk, mila.pk, mathis.pk],
        active_player_index=0,
    )
    barrier = threading.Barrier(3, timeout=3)

    class BarrierClient(MockLLMClient):
        def generate(self, *, system_prompt, **kwargs):
            name = next(
                line[len("[PLAYER:"):].rstrip("]").strip()
                for line in system_prompt.splitlines()
                if line.startswith("[PLAYER:")
            )
            barrier.wait()
            if "NOT the active" in system_prompt:
                return _resp(name, action="PASS", public="")
            return _resp(name, action="ACT", public=f"{name} acts.")

    with patch("rpg.services.turn_engine.get_llm_client", return_value=BarrierClient()):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")

    assert result.turn.state == TurnState.COMPLETED
    assert result.turn.executions.filter(state=ExecutionState.COMPLETED).count() == 3


@pytest.mark.django_db
def test_one_shot_nudge_is_snapshotted_and_consumed(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien", pending_nudge="Do not reveal the secret.")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class NudgeClient(MockLLMClient):
        def generate(self, *, system_prompt, **kwargs):
            assert "# ONE-SHOT GM NUDGE" in system_prompt
            assert "Do not reveal the secret." in system_prompt
            return _resp("Lucien", public="Lucien stays vague.")

    with patch("rpg.services.turn_engine.get_llm_client", return_value=NudgeClient()):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="Speak.",
            selected_players=[lucien],
        )

    execution = result.turn.executions.get(player=lucien)
    lucien.refresh_from_db()
    assert execution.nudge_text == "Do not reveal the secret."
    assert lucien.pending_nudge == ""


@pytest.mark.django_db
def test_execution_debug_snapshots_model_prompt_messages_and_raw(mock_backend):
    camp = make_campaign()
    model = make_model("Primary", gateway_model="primary-model")
    lucien = make_player(camp, "Lucien", model_config=model)
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    with patch("rpg.services.turn_engine.get_llm_client", return_value=MockLLMClient()):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )

    execution = result.turn.executions.get(player=lucien)
    assert execution.model_used == "primary-model"
    assert "[PLAYER: Lucien]" in execution.system_prompt_snapshot
    assert isinstance(execution.request_messages, list)
    assert execution.raw_response
    assert execution.latency_ms is not None


@pytest.mark.django_db
def test_retry_can_use_fallback_model(mock_backend):
    camp = make_campaign()
    primary = make_model("Primary", gateway_model="primary-model")
    fallback = make_model("Fallback", gateway_model="fallback-model")
    lucien = make_player(
        camp,
        "Lucien",
        model_config=primary,
        fallback_model_config=fallback,
    )
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class FailThenClient(MockLLMClient):
        def __init__(self):
            self.models = []
        def generate(self, *, model, **kwargs):
            self.models.append(model)
            if model == "primary-model":
                raise RuntimeError("provider down")
            return _resp("Lucien", public="Fallback answer.")

    client = FailThenClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )
        execution = result.turn.executions.get(player=lucien)
        assert execution.state == ExecutionState.FAILED
        turn_engine.retry_execution(execution, model_config=fallback)

    execution.refresh_from_db()
    assert execution.state == ExecutionState.COMPLETED
    assert execution.model_used == "fallback-model"
    assert client.models == ["primary-model", "fallback-model"]


@pytest.mark.django_db
def test_regen_and_restore_keep_message_version_history(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])

    class VersionsClient(MockLLMClient):
        def __init__(self):
            self.calls = 0
        def generate(self, **kwargs):
            self.calls += 1
            return _resp("Lucien", public="Version one." if self.calls == 1 else "Version two.")

    client = VersionsClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="go",
            selected_players=[lucien],
        )
        execution = result.turn.executions.get(player=lucien)
        message = Message.objects.get(execution=execution, visibility=Visibility.PUBLIC)
        assert [r.content for r in message.revisions.all()] == ["Version one."]

        turn_engine.regenerate_execution(execution)

    message.refresh_from_db()
    revisions = list(message.revisions.order_by("revision_index"))
    assert [r.content for r in revisions] == ["Version one.", "Version two."]

    turn_engine.restore_message_revision(message=message, revision=revisions[0])
    message.refresh_from_db()
    assert message.content == "Version one."
    assert message.revisions.count() == 3
    assert message.revisions.order_by("-revision_index").first().reason == "RESTORE"


@pytest.mark.django_db
def test_undo_latest_round_turn_restores_active_player(mock_backend):
    camp = make_campaign()
    lucien, mila, _ = make_three_players(camp)
    scene = make_scene(
        camp,
        mode=TurnMode.ROUND,
        participants=[lucien, mila],
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    with patch("rpg.services.turn_engine.get_llm_client", return_value=RecordingClient()):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")

    scene.refresh_from_db()
    assert scene.active_player_index == 1
    turn_id = result.turn.pk

    turn_engine.undo_latest_public_turn(scene)

    scene.refresh_from_db()
    assert scene.active_player_index == 0
    assert not Turn.objects.filter(pk=turn_id).exists()
    assert not Message.objects.filter(turn_id=turn_id).exists()



@pytest.mark.django_db
def test_first_regen_of_legacy_message_preserves_old_version(mock_backend):
    camp = make_campaign()
    lucien = make_player(camp, "Lucien")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[lucien])
    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.MANUAL,
        state=TurnState.COMPLETED,
        participants=[lucien.pk],
    )
    execution = TurnExecution.objects.create(
        turn=turn,
        player=lucien,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
        history_message_ids=[],
    )
    legacy = Message.objects.create(
        campaign=camp,
        scene=scene,
        turn=turn,
        execution=execution,
        author_type=AuthorType.PLAYER,
        author_player=lucien,
        content="Legacy visible answer.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )
    assert legacy.revisions.count() == 0

    class LegacyRegenClient(MockLLMClient):
        def generate(self, **kwargs):
            return _resp("Lucien", public="Fresh answer.")

    with patch(
        "rpg.services.turn_engine.get_llm_client",
        return_value=LegacyRegenClient(),
    ):
        turn_engine.regenerate_execution(execution)

    legacy.refresh_from_db()
    revisions = list(legacy.revisions.order_by("revision_index"))
    assert legacy.content == "Fresh answer."
    assert [revision.content for revision in revisions] == [
        "Legacy visible answer.",
        "Fresh answer.",
    ]
    assert [revision.reason for revision in revisions] == ["ORIGINAL", "REGEN"]



@pytest.mark.django_db
def test_manual_chat_turn_waits_without_calling_llm(mock_backend):
    campaign = make_campaign(system_prompt="Campaign rules.")
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_label="ChatGPT",
        manual_chat_url="https://chatgpt.com/c/example",
        manual_chat_context_mode=ManualChatContextMode.FULL,
    )
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[lucien],
    )

    with patch("rpg.services.turn_engine.get_llm_client") as get_client:
        result = turn_engine.start_turn(
            scene=scene,
            gm_message_text="Что ты делаешь?",
            selected_players=[lucien],
        )

    get_client.assert_not_called()
    execution = result.turn.executions.get(player=lucien)
    lucien.refresh_from_db()
    result.turn.refresh_from_db()

    assert execution.state == ExecutionState.WAITING_EXTERNAL
    assert execution.transport == PlayerTransport.MANUAL_CHAT
    assert execution.external_context_mode == ManualChatContextMode.FULL
    assert execution.external_is_bootstrap is False
    assert "# MRAZ MANUAL CHAT BRIDGE" in execution.external_prompt
    assert "Campaign rules." in execution.external_prompt
    assert "Что ты делаешь?" in execution.external_prompt
    assert "RESPONSE CONTRACT" in execution.external_prompt
    assert lucien.status == PlayerStatus.WAITING_EXTERNAL
    assert result.turn.state == TurnState.RUNNING
    assert not Message.objects.filter(
        execution=execution,
        author_type=AuthorType.PLAYER,
    ).exists()


@pytest.mark.django_db
def test_pasted_manual_chat_response_completes_execution(mock_backend):
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.FULL,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[lucien],
    )
    execution = result.turn.executions.get(player=lucien)

    imported = turn_engine.submit_external_response(
        execution=execution,
        raw_text=(
            '{"action_type":"ACT","public":"Люсьен кивает.",'
            '"private_to_gm":"секрет"}'
        ),
    )

    execution.refresh_from_db()
    imported.turn.refresh_from_db()
    lucien.refresh_from_db()

    assert execution.state == ExecutionState.COMPLETED
    assert execution.action_type == "ACT"
    assert imported.turn.state == TurnState.COMPLETED
    assert lucien.status == PlayerStatus.IDLE
    assert execution.raw_response.startswith('{"action_type":"ACT"')
    assert execution.external_synced_message_ids
    assert Message.objects.filter(
        execution=execution,
        visibility=Visibility.PUBLIC,
        content="Люсьен кивает.",
    ).exists()
    assert Message.objects.filter(
        execution=execution,
        visibility=Visibility.PRIVATE_GM_PLAYER,
        content="секрет",
    ).exists()


@pytest.mark.django_db
def test_rejected_manual_chat_response_stays_waiting_for_repaste(mock_backend):
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[lucien],
    )
    execution = result.turn.executions.get(player=lucien)

    with pytest.raises(ValidationError, match="External response rejected"):
        turn_engine.submit_external_response(
            execution=execution,
            raw_text=(
                '{"action_type":"ACT","public":"' + ("x" * 1201) + '",'
                '"private_to_gm":""}'
            ),
        )

    execution.refresh_from_db()
    result.turn.refresh_from_db()
    lucien.refresh_from_db()

    assert execution.state == ExecutionState.WAITING_EXTERNAL
    assert "too long" in execution.error
    assert execution.raw_response
    assert result.turn.state == TurnState.RUNNING
    assert lucien.status == PlayerStatus.WAITING_EXTERNAL
    assert not Message.objects.filter(
        execution=execution,
        author_type=AuthorType.PLAYER,
    ).exists()


@pytest.mark.django_db
def test_chat_memory_bootstrap_then_delta_only_sends_new_context(mock_backend):
    campaign = make_campaign(system_prompt="BIG STATIC CAMPAIGN RULES")
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
        manual_chat_label="Persistent Chat",
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    first = turn_engine.start_turn(
        scene=scene,
        gm_message_text="Первый мастерский ввод.",
        selected_players=[lucien],
    )
    first_execution = first.turn.executions.get(player=lucien)
    assert first_execution.external_is_bootstrap is True
    assert "BIG STATIC CAMPAIGN RULES" in first_execution.external_prompt
    assert "## SYSTEM PROMPT" in first_execution.external_prompt

    turn_engine.submit_external_response(
        execution=first_execution,
        raw_text='{"action_type":"ACT","public":"Первый ответ.","private_to_gm":""}',
    )
    lucien.refresh_from_db()
    assert lucien.manual_chat_initialized is True

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content="[OOC META]\nПомни про красный ключ.",
        visibility=Visibility.PRIVATE_GM_PLAYER,
        private_player=lucien,
    )

    second = turn_engine.start_turn(
        scene=scene,
        gm_message_text="Второй мастерский ввод.",
        selected_players=[lucien],
    )
    second_execution = second.turn.executions.get(player=lucien)

    assert second_execution.external_is_bootstrap is False
    assert "MRAZ MANUAL CHAT BRIDGE · DELTA" in second_execution.external_prompt
    assert "## SYSTEM PROMPT" not in second_execution.external_prompt
    assert "BIG STATIC CAMPAIGN RULES" not in second_execution.external_prompt
    assert "LAST RESPONSE ACCEPTED BY THE APPLICATION" in second_execution.external_prompt
    assert "Первый ответ." in second_execution.external_prompt
    assert "Помни про красный ключ." in second_execution.external_prompt
    assert "Второй мастерский ввод." in second_execution.external_prompt
    assert "Первый мастерский ввод." not in second_execution.external_prompt


@pytest.mark.django_db
def test_mixed_round_waits_for_manual_chat_then_advances_once(mock_backend):
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
    )
    mila = make_player(campaign, "Mila")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[lucien, mila],
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    client = RecordingClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go")

    assert client.calls == ["Mila"]
    lucien_execution = result.turn.executions.get(player=lucien)
    mila_execution = result.turn.executions.get(player=mila)
    result.turn.refresh_from_db()
    scene.refresh_from_db()

    assert lucien_execution.state == ExecutionState.WAITING_EXTERNAL
    assert mila_execution.state == ExecutionState.COMPLETED
    assert result.turn.state == TurnState.RUNNING
    assert scene.active_player_index == 0

    turn_engine.submit_external_response(
        execution=lucien_execution,
        raw_text='{"action_type":"ACT","public":"Lucien acts.","private_to_gm":""}',
    )

    result.turn.refresh_from_db()
    scene.refresh_from_db()
    assert result.turn.state == TurnState.COMPLETED
    assert scene.active_player_index == 1



@pytest.mark.django_db
def test_pending_manual_chat_blocks_overlapping_model_turns(mock_backend):
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    first = turn_engine.start_turn(
        scene=scene,
        gm_message_text="first",
        selected_players=[lucien],
    )
    assert first.turn.executions.get(player=lucien).state == ExecutionState.WAITING_EXTERNAL

    with pytest.raises(ValidationError, match="waiting for a pasted response"):
        turn_engine.start_turn(
            scene=scene,
            gm_message_text="second",
            selected_players=[lucien],
        )

    assert scene.turns.count() == 1



@pytest.mark.django_db
def test_undo_manual_chat_memory_turn_forces_fresh_bootstrap(mock_backend):
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
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
        raw_text='{"action_type":"ACT","public":"Done.","private_to_gm":""}',
    )

    lucien.refresh_from_db()
    assert lucien.manual_chat_initialized is True

    turn_engine.undo_latest_public_turn(scene)

    lucien.refresh_from_db()
    assert lucien.manual_chat_initialized is False



@pytest.mark.django_db
def test_chat_memory_url_change_forces_new_bootstrap(mock_backend):
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
        manual_chat_label="ChatGPT",
        manual_chat_url="https://chatgpt.com/c/one",
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    first = turn_engine.start_turn(
        scene=scene,
        gm_message_text="first",
        selected_players=[lucien],
    )
    first_execution = first.turn.executions.get(player=lucien)
    turn_engine.submit_external_response(
        execution=first_execution,
        raw_text='{"action_type":"ACT","public":"one","private_to_gm":""}',
    )

    lucien.manual_chat_url = "https://chatgpt.com/c/two"
    lucien.save(update_fields=["manual_chat_url", "updated_at"])

    second = turn_engine.start_turn(
        scene=scene,
        gm_message_text="second",
        selected_players=[lucien],
    )
    second_execution = second.turn.executions.get(player=lucien)

    assert second_execution.external_is_bootstrap is True
    assert "## SYSTEM PROMPT" in second_execution.external_prompt
    assert second_execution.external_chat_url.endswith("/two")



@pytest.mark.django_db
def test_undo_waiting_manual_turn_resets_player_status(mock_backend):
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])

    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[lucien],
    )
    lucien.refresh_from_db()
    assert lucien.status == PlayerStatus.WAITING_EXTERNAL

    turn_engine.undo_latest_public_turn(scene)

    lucien.refresh_from_db()
    assert lucien.status == PlayerStatus.IDLE
    assert not Turn.objects.filter(pk=result.turn.pk).exists()



@pytest.mark.django_db
def test_manual_chat_response_cannot_be_imported_twice(mock_backend):
    campaign = make_campaign()
    lucien = make_player(
        campaign,
        "Lucien",
        transport=PlayerTransport.MANUAL_CHAT,
    )
    scene = make_scene(campaign, mode=TurnMode.MANUAL, participants=[lucien])
    result = turn_engine.start_turn(
        scene=scene,
        gm_message_text="go",
        selected_players=[lucien],
    )
    execution = result.turn.executions.get(player=lucien)
    raw = '{"action_type":"ACT","public":"Один ответ.","private_to_gm":""}'

    turn_engine.submit_external_response(execution=execution, raw_text=raw)

    with pytest.raises(ValidationError, match="not waiting"):
        turn_engine.submit_external_response(execution=execution, raw_text=raw)

    assert Message.objects.filter(
        execution=execution,
        author_type=AuthorType.PLAYER,
        visibility=Visibility.PUBLIC,
    ).count() == 1


@pytest.mark.django_db
def test_retroactive_public_regen_invalidates_persistent_manual_chat_memory(mock_backend):
    campaign = make_campaign()
    manual = make_player(
        campaign,
        "Manual",
        transport=PlayerTransport.MANUAL_CHAT,
        manual_chat_context_mode=ManualChatContextMode.CHAT_MEMORY,
        manual_chat_initialized=True,
    )
    api = make_player(campaign, "API")
    scene = make_scene(
        campaign,
        mode=TurnMode.MANUAL,
        participants=[manual, api],
    )

    class RegenClient(MockLLMClient):
        def generate(self, **kwargs):
            return _resp("API", public="Новая версия.")

    turn = Turn.objects.create(
        scene=scene,
        mode=TurnMode.MANUAL,
        state=TurnState.COMPLETED,
        participants=[api.pk],
    )
    execution = TurnExecution.objects.create(
        turn=turn,
        player=api,
        state=ExecutionState.COMPLETED,
        action_type="ACT",
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        turn=turn,
        execution=execution,
        author_type=AuthorType.PLAYER,
        author_player=api,
        content="Старая версия.",
        visibility=Visibility.PUBLIC,
        action_type="ACT",
    )

    with patch(
        "rpg.services.turn_engine.get_llm_client",
        return_value=RegenClient(),
    ):
        turn_engine.regenerate_execution(execution)

    manual.refresh_from_db()
    assert manual.manual_chat_initialized is False
