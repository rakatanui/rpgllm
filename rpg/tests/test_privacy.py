"""Privacy isolation tests for the Context Builder.

This is the security boundary of the application.
"""
import pytest
from rpg.models import AuthorType, Message, Visibility
from rpg.services.context_builder import build_player_context
from rpg.tests.factories import make_campaign, make_player, make_scene, make_three_players


@pytest.mark.django_db
def test_public_visible_to_every_player():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, round_order=[lucien.pk, mila.pk, mathis.pk])
    Message.objects.create(campaign=camp, scene=scene, author_type=AuthorType.GM,
                           content="Hello all", visibility=Visibility.PUBLIC)
    for p in (lucien, mila, mathis):
        ctx = build_player_context(player=p, scene=scene)
        contents = " ".join(m["content"] for m in ctx.messages)
        assert "Hello all" in contents


@pytest.mark.django_db
def test_private_to_mila_is_in_mila_context():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, round_order=[lucien.pk, mila.pk, mathis.pk])
    Message.objects.create(campaign=camp, scene=scene, author_type=AuthorType.GM,
                          content="secret for Mila", visibility=Visibility.PRIVATE_GM_PLAYER,
                          private_player=mila)
    ctx = build_player_context(player=mila, scene=scene)
    contents = " ".join(m["content"] for m in ctx.messages)
    assert "secret for Mila" in contents


@pytest.mark.django_db
def test_private_to_mila_is_NOT_in_lucien_context():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, round_order=[lucien.pk, mila.pk, mathis.pk])
    Message.objects.create(campaign=camp, scene=scene, author_type=AuthorType.GM,
                          content="secret for Mila", visibility=Visibility.PRIVATE_GM_PLAYER,
                          private_player=mila)
    ctx = build_player_context(player=lucien, scene=scene)
    contents = " ".join(m["content"] for m in ctx.messages)
    assert "secret for Mila" not in contents


@pytest.mark.django_db
def test_private_to_mila_is_NOT_in_mathis_context():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, round_order=[lucien.pk, mila.pk, mathis.pk])
    Message.objects.create(campaign=camp, scene=scene, author_type=AuthorType.GM,
                          content="secret for Mila", visibility=Visibility.PRIVATE_GM_PLAYER,
                          private_player=mila)
    ctx = build_player_context(player=mathis, scene=scene)
    contents = " ".join(m["content"] for m in ctx.messages)
    assert "secret for Mila" not in contents


@pytest.mark.django_db
def test_gm_only_never_in_any_player_context():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, round_order=[lucien.pk, mila.pk, mathis.pk])
    Message.objects.create(campaign=camp, scene=scene, author_type=AuthorType.GM,
                          content="gm note", visibility=Visibility.GM_ONLY)
    for p in (lucien, mila, mathis):
        ctx = build_player_context(player=p, scene=scene)
        contents = " ".join(m["content"] for m in ctx.messages)
        assert "gm note" not in contents


@pytest.mark.django_db
def test_player_own_private_response_is_in_their_context():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, round_order=[lucien.pk, mila.pk, mathis.pk])
    # Mila's private response to GM
    Message.objects.create(campaign=camp, scene=scene, author_type=AuthorType.PLAYER,
                          author_player=mila, content="my secret reply",
                          visibility=Visibility.PRIVATE_GM_PLAYER, private_player=mila)
    ctx = build_player_context(player=mila, scene=scene)
    contents = " ".join(m["content"] for m in ctx.messages)
    assert "my secret reply" in contents
    # Lucien must NOT see it
    ctx_l = build_player_context(player=lucien, scene=scene)
    contents_l = " ".join(m["content"] for m in ctx_l.messages)
    assert "my secret reply" not in contents_l


@pytest.mark.django_db
def test_private_to_gm_prompt_requires_material_secret():
    camp = make_campaign()
    player = make_player(camp, "Lucien")
    scene = make_scene(camp)

    ctx = build_player_context(player=player, scene=scene)

    assert 'Use "private_to_gm" sparingly.' in ctx.system_prompt
    assert "Never duplicate or paraphrase the public response there." in ctx.system_prompt
    assert "concealed intention" in ctx.system_prompt
