"""Static checks for human-client live status polling."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "rpg" / "templates" / "rpg"


def test_human_client_uses_status_polling_without_cross_tab_reload_loop():
    client = (TEMPLATES / "human_player.html").read_text(encoding="utf-8")

    assert "human_player_status" in client
    assert "function humanCheckStatus" in client
    assert "humanStatusSignature" in client
    assert "humanPollAll(true)" in client
    assert "window.location.reload()" not in client
    assert "BroadcastChannel" not in client
    assert "mraz.human.turn." not in client
    assert "● YOUR TURN" in client


def test_gm_scene_no_longer_broadcasts_player_turns():
    scene = (TEMPLATES / "scene.html").read_text(encoding="utf-8")
    players = (TEMPLATES / "_players.html").read_text(encoding="utf-8")

    assert "publishHumanTurnSignals" not in scene
    assert "mraz-human-turns-v1" not in scene
    assert "data-human-wait-player-id" not in players
    assert "data-human-wait-execution-id" not in players
