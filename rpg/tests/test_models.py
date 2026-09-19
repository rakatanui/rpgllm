"""Tests for model-level visibility constraints."""
import pytest
from django.core.exceptions import ValidationError
from rpg.models import AuthorType, CharacterAppearance, Message, SceneParticipant, Visibility
from rpg.tests.factories import make_campaign, make_player, make_scene, make_three_players


@pytest.mark.django_db
def test_private_requires_private_player():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp)
    m = Message(campaign=camp, scene=scene, author_type=AuthorType.GM,
                content="x", visibility=Visibility.PRIVATE_GM_PLAYER)
    with pytest.raises(ValidationError):
        m.full_clean()


@pytest.mark.django_db
def test_public_cannot_have_private_player():
    camp = make_campaign()
    lucien, mila, mathis = make_three_players(camp)
    scene = make_scene(camp)
    m = Message(campaign=camp, scene=scene, author_type=AuthorType.GM,
                content="x", visibility=Visibility.PUBLIC, private_player=mila)
    with pytest.raises(ValidationError):
        m.full_clean()


@pytest.mark.django_db
def test_player_author_requires_player():
    camp = make_campaign()
    scene = make_scene(camp)
    m = Message(campaign=camp, scene=scene, author_type=AuthorType.PLAYER,
                content="x", visibility=Visibility.PUBLIC)
    with pytest.raises(ValidationError):
        m.full_clean()

@pytest.mark.django_db
def test_round_order_rejects_duplicates():
    camp = make_campaign()
    player = make_player(camp, "P")
    scene = make_scene(camp, round_order=[player.pk, player.pk])
    with pytest.raises(ValidationError):
        scene.full_clean()


@pytest.mark.django_db
def test_round_order_rejects_foreign_player():
    camp_a = make_campaign("A")
    camp_b = make_campaign("B")
    local = make_player(camp_a, "Local")
    foreign = make_player(camp_b, "Foreign")
    scene = make_scene(camp_a, round_order=[local.pk, foreign.pk])
    with pytest.raises(ValidationError):
        scene.full_clean()


@pytest.mark.django_db
def test_round_order_rejects_missing_player():
    camp = make_campaign()
    local = make_player(camp, "Local")
    scene = make_scene(camp, round_order=[local.pk, 999999])
    with pytest.raises(ValidationError):
        scene.full_clean()


@pytest.mark.django_db
def test_scene_participant_rejects_foreign_current_appearance():
    camp = make_campaign()
    local = make_player(camp, "Local")
    other = make_player(camp, "Other")
    scene = make_scene(camp, participants=[local, other])
    foreign_form = CharacterAppearance.objects.create(
        player=other,
        name="Foreign form",
        is_primary=True,
    )
    participation = SceneParticipant.objects.get(scene=scene, player=local)
    participation.current_appearance = foreign_form

    with pytest.raises(ValidationError):
        participation.full_clean()



@pytest.mark.django_db
def test_scene_participant_access_tokens_are_unique():
    camp = make_campaign()
    first = make_player(camp, "First")
    second = make_player(camp, "Second")
    scene = make_scene(camp, participants=[first, second])

    tokens = list(
        SceneParticipant.objects.filter(scene=scene)
        .order_by("pk")
        .values_list("human_access_token", flat=True)
    )

    assert len(tokens) == 2
    assert all(tokens)
    assert len(set(tokens)) == 2
