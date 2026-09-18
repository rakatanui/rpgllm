"""Tests for the seed_demo command (idempotent)."""
import pytest
from django.core.management import call_command
from rpg.models import Campaign, Player, Scene, TurnMode


@pytest.mark.django_db
def test_seed_demo_creates_data():
    call_command("seed_demo")
    assert Campaign.objects.filter(name="МРАЗь").exists()
    scene = Scene.objects.get(name="Test scene")
    assert scene.mode == TurnMode.ROUND
    names = list(Player.objects.values_list("display_name", flat=True))
    assert {"Lucien", "Mila", "Mathis"}.issubset(set(names))


@pytest.mark.django_db
def test_seed_demo_is_idempotent():
    call_command("seed_demo")
    n1 = Player.objects.count()
    n1c = Campaign.objects.count()
    n1s = Scene.objects.count()
    call_command("seed_demo")
    n2 = Player.objects.count()
    n2c = Campaign.objects.count()
    n2s = Scene.objects.count()
    assert n2 == n1
    assert n2c == n1c
    assert n2s == n1s