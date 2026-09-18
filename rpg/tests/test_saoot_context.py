"""SAOOT context and rendering rules."""
import pytest

from rpg.models import AuthorType, Message, TurnMode, Visibility
from rpg.services.context_builder import build_player_context
from rpg.templatetags.rpg_extras import saoot_format
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
