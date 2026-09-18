"""Privacy isolation tests for the Context Builder.

This is the security boundary of the application.
"""
import pytest
from django.test import override_settings

from rpg.models import AuthorType, LoreEntry, LoreScope, Message, Visibility
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



@pytest.mark.django_db
def test_global_lore_is_visible_to_every_player():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp, round_order=[lucien.pk, mila.pk, mathis.pk])
    LoreEntry.objects.create(
        campaign=camp,
        title="Common supernatural law",
        category="GENERAL",
        content="All supernatural beings recognize the Veil.",
        scope=LoreScope.GLOBAL,
    )

    for player in (lucien, mila, mathis):
        ctx = build_player_context(player=player, scene=scene)
        assert "Common supernatural law" in ctx.system_prompt
        assert "All supernatural beings recognize the Veil." in ctx.system_prompt


@pytest.mark.django_db
def test_scene_lore_is_only_visible_in_assigned_scene():
    camp = make_campaign()
    player = make_player(camp, "Lucien")
    rio = make_scene(camp, name="Rio")
    gdansk = make_scene(camp, name="Gdansk")
    lore = LoreEntry.objects.create(
        campaign=camp,
        title="Rio safehouse",
        content="The safehouse entrance is behind the blue gate.",
        scope=LoreScope.SCENE,
    )
    lore.scenes.add(rio)

    rio_ctx = build_player_context(player=player, scene=rio)
    gdansk_ctx = build_player_context(player=player, scene=gdansk)

    assert "Rio safehouse" in rio_ctx.system_prompt
    assert "Rio safehouse" not in gdansk_ctx.system_prompt


@pytest.mark.django_db
def test_player_lore_is_not_leaked_to_other_players():
    camp = make_campaign()
    lucien, mila, _ = make_three_players(camp)
    scene = make_scene(camp)
    lore = LoreEntry.objects.create(
        campaign=camp,
        title="Lucien-only secret",
        content="Lucien knows the hidden name.",
        scope=LoreScope.PLAYER,
    )
    lore.players.add(lucien)

    lucien_ctx = build_player_context(player=lucien, scene=scene)
    mila_ctx = build_player_context(player=mila, scene=scene)

    assert "Lucien-only secret" in lucien_ctx.system_prompt
    assert "hidden name" in lucien_ctx.system_prompt
    assert "Lucien-only secret" not in mila_ctx.system_prompt
    assert "hidden name" not in mila_ctx.system_prompt


@pytest.mark.django_db
def test_compact_memories_are_always_in_system_prompt():
    camp = make_campaign(shared_memory="The group owes Bruno a favor.")
    player = make_player(
        camp,
        "Lucien",
        memory_summary="I secretly promised Mila I would protect her.",
    )
    scene = make_scene(
        camp,
        memory_summary="The warehouse alarm has already been disabled.",
    )

    ctx = build_player_context(player=player, scene=scene)

    assert "The group owes Bruno a favor." in ctx.system_prompt
    assert "I secretly promised Mila I would protect her." in ctx.system_prompt
    assert "The warehouse alarm has already been disabled." in ctx.system_prompt


@pytest.mark.django_db
@override_settings(CONTEXT_HISTORY_MAX_CHARS=220)
def test_history_budget_keeps_newest_contiguous_visible_tail():
    camp = make_campaign()
    player = make_player(camp, "Lucien")
    scene = make_scene(camp)

    Message.objects.create(
        campaign=camp,
        scene=scene,
        author_type=AuthorType.GM,
        content="OLD-" + ("a" * 120),
        visibility=Visibility.PUBLIC,
    )
    Message.objects.create(
        campaign=camp,
        scene=scene,
        author_type=AuthorType.GM,
        content="MIDDLE-" + ("b" * 80),
        visibility=Visibility.PUBLIC,
    )
    Message.objects.create(
        campaign=camp,
        scene=scene,
        author_type=AuthorType.GM,
        content="NEWEST",
        visibility=Visibility.PUBLIC,
    )

    ctx = build_player_context(player=player, scene=scene)
    text = " ".join(message["content"] for message in ctx.messages)

    assert "NEWEST" in text
    assert "OLD-" not in text
    assert "# CONTEXT NOTE" in ctx.system_prompt


@pytest.mark.django_db
@override_settings(CONTEXT_LORE_MAX_CHARS=180)
def test_lore_budget_prefers_lower_priority_entries():
    camp = make_campaign()
    player = make_player(camp, "Lucien")
    scene = make_scene(camp)

    LoreEntry.objects.create(
        campaign=camp,
        title="Critical lore",
        content="CRITICAL-" + ("x" * 60),
        scope=LoreScope.GLOBAL,
        priority=10,
    )
    LoreEntry.objects.create(
        campaign=camp,
        title="Low priority lore",
        content="LOW-PRIORITY-" + ("y" * 120),
        scope=LoreScope.GLOBAL,
        priority=200,
    )

    ctx = build_player_context(player=player, scene=scene)

    assert "Critical lore" in ctx.system_prompt
    assert "CRITICAL-" in ctx.system_prompt
    assert "Low priority lore" not in ctx.system_prompt
