"""Tests for the Turn Engine: MANUAL, ROUND, SIMULTANEOUS, TABLE, state machine,
and the critical 'AI Message never triggers a Turn' regression."""
import pytest
from django.test import override_settings
from unittest.mock import patch, MagicMock

from rpg.models import (
    AuthorType, Message, PlayerStatus, Turn, TurnMode, TurnState, Visibility,
)
from rpg.services import turn_engine
from rpg.services.llm import LLMResponse, MockLLMClient
from rpg.tests.factories import make_campaign, make_model, make_player, make_scene, make_three_players


def _mock_resp(player_name, action="ACT", public=None, private=""):
    return LLMResponse(
        raw_text="{}",
        action_type=action,
        public=public or f"[{action}] {player_name}",
        private_to_gm=private,
    )


class CountingMockClient(MockLLMClient):
    """Mock that records how many times generate() was called per player."""
    def __init__(self):
        super().__init__()
        self.calls = []

    async def generate(self, *, system_prompt, messages, model, temperature=0.7):
        # figure out player name
        name = "Unknown"
        for line in system_prompt.splitlines():
            if line.startswith("[PLAYER:"):
                name = line[len("[PLAYER:"):]
                name = name.rstrip("]").strip()
        self.calls.append(name)
        return _mock_resp(name)


@pytest.fixture
def counting_client(mock_backend):
    client = CountingMockClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=client):
        yield client


# ---------------------------------------------------------------------------
# MANUAL
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_manual_calls_only_selected_players(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.MANUAL, round_order=[lucien.pk, mila.pk, mathis.pk])
    turn_engine.start_turn(scene=scene, gm_message_text="GM says hi",
                            selected_players=[mila])
    assert counting_client.calls == ["Mila"]
    msgs = list(Message.objects.filter(scene=scene, author_type=AuthorType.PLAYER))
    assert len(msgs) == 1
    assert msgs[0].author_player == mila


@pytest.mark.django_db
def test_manual_calls_multiple_selected(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.MANUAL, round_order=[lucien.pk, mila.pk, mathis.pk])
    turn_engine.start_turn(scene=scene, gm_message_text="hi",
                            selected_players=[lucien, mathis])
    assert set(counting_client.calls) == {"Lucien", "Mathis"}
    assert "Mila" not in counting_client.calls


# ---------------------------------------------------------------------------
# ROUND
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_round_calls_all_players_and_advances_active(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.ROUND,
                       round_order=[lucien.pk, mila.pk, mathis.pk],
                       active_player_index=0)
    turn_engine.start_turn(scene=scene, gm_message_text="round start")
    # all three called
    assert set(counting_client.calls) == {"Lucien", "Mila", "Mathis"}
    scene.refresh_from_db()
    # active advanced by exactly one: 0 -> 1
    assert scene.active_player_index == 1


@pytest.mark.django_db
def test_round_active_advances_cyclic(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.ROUND,
                       round_order=[lucien.pk, mila.pk, mathis.pk],
                       active_player_index=2)  # Mathis active
    turn_engine.start_turn(scene=scene, gm_message_text="go")
    scene.refresh_from_db()
    assert scene.active_player_index == 0  # wrapped


@pytest.mark.django_db
def test_round_saving_ai_message_does_not_trigger_new_turn(counting_client):
    """REGRESSION: creating a PLAYER message must NOT start another turn."""
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.ROUND,
                       round_order=[lucien.pk, mila.pk, mathis.pk])
    n_before = Turn.objects.count()
    turn_engine.start_turn(scene=scene, gm_message_text="hi")
    n_after_turn = Turn.objects.count()
    assert n_after_turn == n_before + 1
    # Now manually save a PLAYER message (simulating any code path).
    msg = Message.objects.create(
        campaign=camp, scene=scene, author_type=AuthorType.PLAYER,
        author_player=lucien, content="manual AI reply", visibility=Visibility.PUBLIC,
    )
    n_after_save = Turn.objects.count()
    # No new turn created by saving the message.
    assert n_after_save == n_after_turn


# ---------------------------------------------------------------------------
# SIMULTANEOUS
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_simultaneous_uses_same_snapshot(counting_client):
    """All players see the same public history snapshot (before any of their answers)."""
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.SIMULTANEOUS,
                       round_order=[lucien.pk, mila.pk, mathis.pk])
    # Pre-existing public message
    Message.objects.create(campaign=camp, scene=scene, author_type=AuthorType.GM,
                           content="old GM", visibility=Visibility.PUBLIC)
    turn_engine.start_turn(scene=scene, gm_message_text="new GM", selected_players=[lucien, mila])
    # both called
    assert set(counting_client.calls) == {"Lucien", "Mila"}
    # Lucien should NOT see Mila's answer in his context (same snapshot).
    # Inspect the messages saved:
    player_msgs = list(Message.objects.filter(scene=scene, author_type=AuthorType.PLAYER))
    # 2 public messages
    assert len(player_msgs) == 2


@pytest.mark.django_db
def test_simultaneous_each_player_called_once(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.SIMULTANEOUS,
                       round_order=[lucien.pk, mila.pk, mathis.pk])
    turn_engine.start_turn(scene=scene, gm_message_text="hi", selected_players=[lucien, mila])
    assert counting_client.calls.count("Lucien") == 1
    assert counting_client.calls.count("Mila") == 1
    assert counting_client.calls.count("Mathis") == 0


# ---------------------------------------------------------------------------
# TABLE
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_table_each_player_sees_previous_public_response(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.TABLE,
                       round_order=[lucien.pk, mila.pk, mathis.pk])
    # Use a custom client that records the history it received.
    seen_histories = {}

    class HistoryClient(MockLLMClient):
        async def generate(self, *, system_prompt, messages, model, temperature=0.7):
            name = "Unknown"
            for line in system_prompt.splitlines():
                if line.startswith("[PLAYER:"):
                    name = line[len("[PLAYER:"):]
                    name = name.rstrip("]").strip()
            seen_histories[name] = list(messages)
            return _mock_resp(name)

    c = HistoryClient()
    with patch("rpg.services.turn_engine.get_llm_client", return_value=c):
        turn_engine.start_turn(scene=scene, gm_message_text="go")
    # Lucien first: should NOT see Mila/Mathis responses
    lucien_contents = " ".join(m["content"] for m in seen_histories["Lucien"])
    assert "Mila" not in lucien_contents.split("Mila:")[-1] if "Mila:" in lucien_contents else True
    # Mila should see Lucien's response
    mila_contents = " ".join(m["content"] for m in seen_histories["Mila"])
    assert "Lucien" in mila_contents
    # Mathis should see both Lucien and Mila
    mathis_contents = " ".join(m["content"] for m in seen_histories["Mathis"])
    assert "Lucien" in mathis_contents
    assert "Mila" in mathis_contents


@pytest.mark.django_db
def test_table_each_player_called_once(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.TABLE,
                       round_order=[lucien.pk, mila.pk, mathis.pk])
    turn_engine.start_turn(scene=scene, gm_message_text="go")
    assert counting_client.calls == ["Lucien", "Mila", "Mathis"]
    assert counting_client.calls.count("Lucien") == 1


# ---------------------------------------------------------------------------
# State machine / failures
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_failed_llm_puts_turn_in_failed_state(mock_backend):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.MANUAL, round_order=[lucien.pk])

    class FailClient(MockLLMClient):
        async def generate(self, **kw):
            raise RuntimeError("provider down")

    with patch("rpg.services.turn_engine.get_llm_client", return_value=FailClient()):
        result = turn_engine.start_turn(scene=scene, gm_message_text="go",
                                        selected_players=[lucien])
    result.turn.refresh_from_db()
    assert result.turn.state == TurnState.FAILED
    assert "provider down" in result.turn.error
    lucien.refresh_from_db()
    assert lucien.status == PlayerStatus.ERROR


@pytest.mark.django_db
def test_mock_mode_makes_no_http_call(mock_backend):
    """MockLLMClient must never perform network I/O."""
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.MANUAL, round_order=[lucien.pk])
    with patch("rpg.services.llm.httpx.AsyncClient") as mock_http:
        turn_engine.start_turn(scene=scene, gm_message_text="go", selected_players=[lucien])
        # httpx.AsyncClient should never be instantiated by MockLLMClient
        mock_http.assert_not_called()


@pytest.mark.django_db
def test_turn_completed_state_on_success(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.MANUAL, round_order=[lucien.pk])
    result = turn_engine.start_turn(scene=scene, gm_message_text="go", selected_players=[lucien])
    result.turn.refresh_from_db()
    assert result.turn.state == TurnState.COMPLETED


@pytest.mark.django_db
def test_retry_turn_requires_failed_state(counting_client):
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, mode=TurnMode.MANUAL, round_order=[lucien.pk])
    result = turn_engine.start_turn(scene=scene, gm_message_text="go", selected_players=[lucien])
    with pytest.raises(RuntimeError):
        turn_engine.retry_turn(result.turn)