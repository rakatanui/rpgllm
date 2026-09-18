"""Tests for the structured response parser / fallback."""
import pytest
from rpg.services.llm import parse_structured_response


def test_parse_valid_json():
    r = parse_structured_response('{"action_type": "ACT", "public": "I open the door.", "private_to_gm": "I watch Thomas."}')
    assert r.action_type == "ACT"
    assert r.public == "I open the door."
    assert r.private_to_gm == "I watch Thomas."


def test_parse_pass():
    r = parse_structured_response('{"action_type": "PASS", "public": "", "private_to_gm": ""}')
    assert r.action_type == "PASS"


def test_parse_act_out_of_turn():
    r = parse_structured_response('{"action_type": "ACT_OUT_OF_TURN", "public": "I speak up.", "private_to_gm": ""}')
    assert r.action_type == "ACT_OUT_OF_TURN"


def test_parse_invalid_action_falls_back_to_act():
    r = parse_structured_response('{"action_type": "DANCE", "public": "x"}')
    assert r.action_type == "ACT"


def test_parse_non_json_fallback_pass():
    r = parse_structured_response("[PASS] Mila")
    assert r.action_type == "PASS"


def test_parse_non_json_fallback_act():
    r = parse_structured_response("I swing my sword.")
    assert r.action_type == "ACT"
    assert r.public == "I swing my sword."
    assert r.private_to_gm == ""


def test_private_never_in_public_for_json():
    r = parse_structured_response('{"action_type": "ACT", "public": "public text", "private_to_gm": "secret"}')
    assert "secret" not in r.public
    assert r.private_to_gm == "secret"


def test_parse_fenced_json():
    r = parse_structured_response('```json\n{"action_type": "ACT", "public": "I move.", "private_to_gm": "x"}\n```')
    assert r.action_type == "ACT"
    assert r.public == "I move."
    assert r.private_to_gm == "x"


def test_parse_fenced_json_no_lang():
    r = parse_structured_response('```\n{"action_type": "PASS", "public": "", "private_to_gm": ""}\n```')
    assert r.action_type == "PASS"


def test_parse_json_embedded_in_prose():
    r = parse_structured_response('Here is my response:\n{"action_type": "ACT", "public": "I attack!", "private_to_gm": ""}\nDone.')
    assert r.action_type == "ACT"
    assert r.public == "I attack!"