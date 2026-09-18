"""SAOOT context and rendering rules."""
import pytest

from rpg.models import AuthorType, Message, TurnMode, Visibility
from rpg.services.context_builder import build_player_context
from rpg.templatetags.rpg_extras import message_format, saoot_format
from rpg.tests.factories import make_campaign, make_player, make_scene


@pytest.mark.django_db
def test_round_context_explains_saoot_and_preserves_action_type_in_history():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[lucien, mila],
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
    )

    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.PLAYER,
        author_player=mila,
        content="Мила перехватывает дверь.",
        visibility=Visibility.PUBLIC,
        action_type="ACT_OUT_OF_TURN",
    )
    Message.objects.create(
        campaign=campaign,
        scene=scene,
        author_type=AuthorType.GM,
        content=(
            f"[[SAOOT:{mila.pk}|Мила]]"
            "Дверь закрывается раньше, чем противник успевает пройти."
            "[[/SAOOT]]"
        ),
        visibility=Visibility.PUBLIC,
    )

    ctx = build_player_context(player=lucien, scene=scene)
    history = "\n".join(item["content"] for item in ctx.messages)

    assert "# ROUND / ACT_OUT_OF_TURN" in ctx.system_prompt
    assert "NO SAOOT marker" in ctx.system_prompt
    assert "treat that declaration as successful" in ctx.system_prompt
    assert "a SAOOT marker for one player does not resolve the others" in ctx.system_prompt
    assert "Мила [ACT_OUT_OF_TURN]" in history
    assert f"[[SAOOT:{mila.pk}|Мила]]" in history


def test_saoot_format_escapes_untrusted_text_and_renders_marker():
    rendered = str(
        saoot_format(
            'До. [[SAOOT:7|Мила]]<script>alert(1)</script>[[/SAOOT]] После.'
        )
    )

    assert '<script>' not in rendered
    assert '&lt;script&gt;' in rendered
    assert 'class="saoot-resolution"' in rendered
    assert "SAOOT · Мила" in rendered
    assert "[[SAOOT:" not in rendered


@pytest.mark.django_db
def test_context_requires_original_language_speech_with_russian_hover_translation():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[lucien],
        round_order=[lucien.pk],
        active_player_index=0,
        dialogue_language="French",
    )

    ctx = build_player_context(player=lucien, scene=scene)

    assert "Default spoken language: French" in ctx.system_prompt
    assert "# DIALOGUE LANGUAGE AND FORMAT" in ctx.system_prompt
    assert "Narration and non-spoken action text" in ctx.system_prompt
    assert "not automatically in Russian" in ctx.system_prompt
    assert "[[SPEECH]]original-language sentence[[RU]]Russian translation[[/SPEECH]]" in ctx.system_prompt
    assert "[[SPEECH]]Je vais vérifier la voiture." in ctx.system_prompt
    assert "Do not print a second visible Russian translation" in ctx.system_prompt


def test_message_format_renders_original_and_hides_russian_in_tooltip():
    rendered = str(
        message_format(
            "Люсьен кивает. "
            "[[SPEECH]]Je vais vérifier la voiture."
            "[[RU]]Я проверю машину.[[/SPEECH]]"
        )
    )

    assert "Люсьен кивает." in rendered
    assert "Je vais vérifier la voiture." in rendered
    assert 'class="translated-speech"' in rendered
    assert 'data-translation="Я проверю машину."' in rendered
    assert "[[SPEECH]]" not in rendered
    assert "[[RU]]" not in rendered


def test_message_format_combines_saoot_and_speech_safely():
    rendered = str(
        message_format(
            "[[SAOOT:7|Мила]]"
            "[[SPEECH]]Arrêtez.<script>x</script>"
            "[[RU]]Стойте.<script>y</script>[[/SPEECH]]"
            "[[/SAOOT]]"
        )
    )

    assert 'class="saoot-resolution"' in rendered
    assert 'class="translated-speech"' in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered



@pytest.mark.django_db
def test_round_prompt_makes_interruptions_rare_and_turns_compact():
    campaign = make_campaign()
    lucien = make_player(campaign, "Люсьен")
    mila = make_player(campaign, "Мила")
    scene = make_scene(
        campaign,
        mode=TurnMode.ROUND,
        participants=[lucien, mila],
        round_order=[lucien.pk, mila.pk],
        active_player_index=0,
        dialogue_language="French",
    )

    ctx = build_player_context(player=mila, scene=scene)

    assert "PASS is the normal and preferred response" in ctx.system_prompt
    assert "ACT_OUT_OF_TURN is an exceptional interruption" in ctx.system_prompt
    assert "Most inactive ROUND responses should therefore be PASS" in ctx.system_prompt
    assert "ONE concise immediate intervention" in ctx.system_prompt
    assert "# RESPONSE DISCIPLINE" in ctx.system_prompt
    assert "HARD LIMITS FOR A NORMAL PUBLIC ACT" in ctx.system_prompt
    assert "no more than 1200 visible characters" in ctx.system_prompt
    assert "no more than 5 paragraphs" in ctx.system_prompt
    assert "no more than 2 direct questions" in ctx.system_prompt
    assert "HARD LIMITS FOR ACT_OUT_OF_TURN" in ctx.system_prompt
    assert "no more than 650 visible characters" in ctx.system_prompt
    assert "no more than 2 paragraphs" in ctx.system_prompt
    assert "no more than 1 direct question" in ctx.system_prompt
    assert "# OOC FEEDBACK" in ctx.system_prompt
    assert "meta-level discussion" in ctx.system_prompt
    assert "it does not create an additional action or a new turn" in ctx.system_prompt
