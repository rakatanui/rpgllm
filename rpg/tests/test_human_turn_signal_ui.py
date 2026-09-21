"""Static checks for GM -> human-client turn signaling."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "rpg" / "templates" / "rpg"


def test_gm_scene_broadcasts_new_human_turns():
    scene = (TEMPLATES / "scene.html").read_text(encoding="utf-8")
    players = (TEMPLATES / "_players.html").read_text(encoding="utf-8")

    assert 'new BroadcastChannel("mraz-human-turns-v1")' in scene
    assert '"mraz.human.turn." + playerId' in scene
    assert "publishHumanTurnSignals()" in scene
    assert 'data-human-wait-player-id="{{ p.pk }}"' in players
    assert 'data-human-wait-execution-id="{{ human_waiting.pk }}"' in players


def test_human_client_forces_one_sync_on_turn_signal():
    client = (TEMPLATES / "human_player.html").read_text(encoding="utf-8")
    panel = (TEMPLATES / "_human_player_panel.html").read_text(encoding="utf-8")

    assert 'new BroadcastChannel("mraz-human-turns-v1")' in client
    assert "function humanHandleTurnSignal" in client
    assert "humanPollAll(true)" in client
    assert 'window.addEventListener("storage"' in client
    assert 'document.addEventListener("visibilitychange"' in client
    assert '"● YOUR TURN · " + humanBaseTitle' in client
    assert 'data-waiting-execution-id="{{ waiting_execution.pk|default:\'\' }}"' in panel
