"""Tests for model-level visibility constraints."""
import pytest
from django.core.exceptions import ValidationError
from rpg.models import AuthorType, Message, Visibility
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