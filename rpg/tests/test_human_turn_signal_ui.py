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
    assert "Human live-region refresh failed" in client
    assert "region.replaceWith(replacement)" in client
    assert "htmx.trigger(region" not in client
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


def test_gm_dashboard_polls_model_state_without_full_page_reload():
    scene = (TEMPLATES / "scene.html").read_text(encoding="utf-8")

    assert 'id="gm-model-live"' in scene
    assert "gm_model_status" in scene
    assert "gm_model_panel" in scene
    assert "pollGmModelStatus" in scene
    assert 'new CustomEvent("mraz:gm-panel-updated")' in scene


def test_mobile_player_can_collapse_upper_controls_without_hiding_scene_status():
    client = (TEMPLATES / "human_player.html").read_text(encoding="utf-8")
    panel = (TEMPLATES / "_human_player_panel.html").read_text(encoding="utf-8")
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert "humanMobileTopStorageKey" in client
    assert "function humanApplyMobileTopState" in client
    assert "function humanToggleMobileTop" in client
    assert "[data-human-mobile-top-toggle]" in client
    assert "humanApplyMobileTopState();" in client

    assert "data-human-mobile-top-toggle" in panel
    assert "human-mobile-trim-wheel" in panel

    assert ".human-mobile-deck-toggle { display:none; }" in base
    assert ".human-player-shell.human-mobile-top-collapsed > .human-character-card { display:none; }" in base
    assert ".human-player-shell.human-mobile-top-collapsed .human-scene-tools .human-composer-card { display:none; }" in base
    assert ".human-player-shell.human-mobile-top-collapsed #human-scene-live > .human-notes-inline { display:none; }" in base
    assert ".human-player-shell.human-mobile-top-collapsed .human-status-card" not in base
