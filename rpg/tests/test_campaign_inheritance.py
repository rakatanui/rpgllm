"""Cross-scene inheritance via the campaign workbench, with player privacy."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from rpg.models import AuthorType, Campaign, Message, Player, Scene, Turn, TurnMode, Visibility
from rpg.services.context_builder import build_player_context, get_player_history_messages, get_scene_lineage
from rpg.tests.factories import make_campaign, make_player, make_scene


def staff(name):
    return get_user_model().objects.create_user(
        username=name, password="a-long-password", is_staff=True,
    )


def session(user):
    client = Client()
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client


def scene_data(name, participants=(), previous=(), **changes):
    return {
        "name": name, "description": "The next chapter", "mode": TurnMode.MANUAL,
        "dialogue_language": "Russian", "memory_summary": "",
        "participants": [str(p.pk) for p in participants],
        "previous_scenes": [str(s.pk) for s in previous],
        **changes,
    }


def write(campaign, scene, text, *, visibility=Visibility.PUBLIC, private=None):
    return Message.objects.create(
        campaign=campaign, scene=scene, author_type=AuthorType.GM,
        visibility=visibility, private_player=private, content=text,
    )


def closed(scene):
    Scene.objects.filter(pk=scene.pk).update(is_closed=True)
    scene.refresh_from_db()
    return scene


@pytest.mark.django_db
def test_followup_form_prefills_source_participants_language_mode_but_not_summary():
    master = staff("inherit-prefill")
    campaign = make_campaign(owner=master)
    a = make_player(campaign, "Alpha")
    b = make_player(campaign, "Bravo")
    earlier = make_scene(
        campaign, "Episode 1", mode=TurnMode.SOFT_ROUND,
        participants=[a, b], memory_summary="Source-only memory",
        dialogue_language="Lithuanian",
    )
    closed(earlier)
    url = reverse("campaign_scene_create", args=[campaign.pk])
    response = session(master).get(f"{url}?from_scene={earlier.pk}")
    assert response.status_code == 200
    form = response.context["form"]
    assert form["previous_scenes"].value() == [earlier.pk]
    assert form["participants"].value() == [a.pk, b.pk]
    assert form["dialogue_language"].value() == "Lithuanian"
    assert form["mode"].value() == TurnMode.SOFT_ROUND
    assert not form["memory_summary"].value()
    assert "only the predecessor scenes they actually participated in" in response.content.decode()

    creation = session(master).post(
        url, scene_data(
            "Episode 2", participants=[a, b], previous=[earlier],
            dialogue_language="Lithuanian", mode=TurnMode.SOFT_ROUND,
        ),
    )
    assert creation.status_code == 302
    followup = Scene.objects.get(campaign=campaign, name="Episode 2")
    assert list(followup.previous_scenes.all()) == [earlier]
    assert followup.memory_summary == ""
    assert followup.dialogue_language == "Lithuanian"
    assert followup.mode == TurnMode.SOFT_ROUND
    assert followup.is_closed is False

    # A new scene issues independent access tokens for the same characters.
    old_token = earlier.scene_participants.get(player=a).human_access_token
    new_token = followup.scene_participants.get(player=a).human_access_token
    assert new_token != old_token


@pytest.mark.django_db
def test_create_joined_scene_preserves_parallel_branches_and_private_knowledge():
    master = staff("inherit-multi")
    campaign = make_campaign(owner=master)
    alice = make_player(campaign, "Alice")
    bob = make_player(campaign, "Bob")
    newcomer = make_player(campaign, "Newcomer")

    first = make_scene(
        campaign, "Alice branch", participants=[alice],
        memory_summary="ALICE-SUMMARY-SECRET", mode=TurnMode.MANUAL,
    )
    write(campaign, first, "ALICE-PUBLIC-OBSERVATION")
    write(campaign, first, "ALICE-PRIVATE-SECRET", visibility=Visibility.PRIVATE_GM_PLAYER, private=alice)
    write(campaign, first, "GM-ONLY-NOTICE", visibility=Visibility.GM_ONLY)
    closed(first)

    second = make_scene(
        campaign, "Bob branch", participants=[bob],
        memory_summary="BOB-SUMMARY-SECRET", mode=TurnMode.MANUAL,
    )
    write(campaign, second, "BOB-PUBLIC-OBSERVATION")
    write(campaign, second, "BOB-PRIVATE-SECRET", visibility=Visibility.PRIVATE_GM_PLAYER, private=bob)
    closed(second)

    url = reverse("campaign_scene_create", args=[campaign.pk])
    response = session(master).post(
        url, scene_data(
            "Branch convergence", participants=[alice, bob, newcomer],
            previous=[first, second]
        ),
    )
    assert response.status_code == 302
    merged = Scene.objects.get(name="Branch convergence", campaign=campaign)
    assert set(merged.previous_scenes.values_list("pk", flat=True)) == {first.pk, second.pk}
    assert list(get_scene_lineage(merged)) == [first, second, merged]

    a_ctx = build_player_context(player=alice, scene=merged)
    b_ctx = build_player_context(player=bob, scene=merged)
    newcomer_ctx = build_player_context(player=newcomer, scene=merged)
    a_history = "\n".join(m["content"] for m in a_ctx.messages)
    b_history = "\n".join(m["content"] for m in b_ctx.messages)
    new_history = "\n".join(m["content"] for m in newcomer_ctx.messages)

    assert "ALICE-SUMMARY-SECRET" in a_ctx.system_prompt
    assert "BOB-SUMMARY-SECRET" not in a_ctx.system_prompt
    assert "ALICE-PUBLIC-OBSERVATION" in a_history
    assert "ALICE-PRIVATE-SECRET" in a_history
    assert "BOB-PUBLIC-OBSERVATION" not in a_history
    assert "GM-ONLY-NOTICE" not in a_history

    assert "BOB-SUMMARY-SECRET" in b_ctx.system_prompt
    assert "ALICE-SUMMARY-SECRET" not in b_ctx.system_prompt
    assert "BOB-PRIVATE-SECRET" in b_history
    assert "ALICE-PRIVATE-SECRET" not in b_history

    for hidden in ("BOB-SUMMARY-SECRET", "ALICE-SUMMARY-SECRET"):
        assert hidden not in newcomer_ctx.system_prompt
    for hidden in (
        "ALICE-PUBLIC-OBSERVATION", "BOB-PUBLIC-OBSERVATION",
        "ALICE-PRIVATE-SECRET", "BOB-PRIVATE-SECRET", "GM-ONLY-NOTICE",
    ):
        assert hidden not in new_history
    assert not get_player_history_messages(player=newcomer, scene=merged)


@pytest.mark.django_db
def test_scene_editor_preserves_and_updates_inherited_sources_before_play():
    master = staff("inherit-edit")
    campaign = make_campaign(owner=master)
    hero = make_player(campaign, "Hero")
    parent_a = closed(make_scene(campaign, "A", participants=[hero]))
    parent_b = closed(make_scene(campaign, "B", participants=[hero]))
    current = make_scene(campaign, "Current", participants=[hero], mode=TurnMode.MANUAL)
    current.previous_scenes.add(parent_a)
    participation = current.scene_participants.get(player=hero)
    client = session(master)

    edit = reverse("campaign_scene_edit", args=[campaign.pk, current.pk])
    initial = client.get(edit)
    assert initial.status_code == 200
    assert initial.context["form"]["previous_scenes"].value() == [parent_a.pk]
    assert "A" in initial.content.decode()
    assert "B" in initial.content.decode()

    result = client.post(
        edit, scene_data("Renamed", participants=[hero], previous=[parent_b])
    )
    assert result.status_code == 302
    current.refresh_from_db()
    assert current.name == "Renamed"
    assert list(current.previous_scenes.all()) == [parent_b]
    assert current.scene_participants.get(player=hero).human_access_token == participation.human_access_token


@pytest.mark.django_db
def test_create_without_previous_history_is_standalone():
    master = staff("inherit-standalone")
    campaign = make_campaign(owner=master)
    hero = make_player(campaign, "Hero")
    existing = make_scene(campaign, "Unrelated", participants=[hero])
    write(campaign, existing, "DO-NOT-INHERIT")
    closed(existing)
    result = session(master).post(
        reverse("campaign_scene_create", args=[campaign.pk]),
        scene_data("Standalone", participants=[hero]),
    )
    assert result.status_code == 302
    scene = Scene.objects.get(campaign=campaign, name="Standalone")
    assert not scene.previous_scenes.exists()
    context = build_player_context(player=hero, scene=scene)
    assert "DO-NOT-INHERIT" not in "\n".join(m["content"] for m in context.messages)


@pytest.mark.django_db
def test_reject_open_and_cross_campaign_predecessors_on_creation():
    master = staff("inherit-invalid")
    own = make_campaign("Own", owner=master)
    foreign = make_campaign("Elsewhere", owner=master)
    hero = make_player(own, "Hero")
    other = closed(make_scene(foreign, "Foreign", participants=[]))
    open_scene = make_scene(own, "Still active", participants=[hero])
    client = session(master)
    url = reverse("campaign_scene_create", args=[own.pk])

    for source in (other, open_scene):
        result = client.post(
            url, scene_data("Injected", participants=[hero], previous=[source])
        )
        assert result.status_code == 200
        assert "previous_scenes" in result.context["form"].errors
        assert not own.scenes.filter(name="Injected").exists()


@pytest.mark.django_db
def test_invalid_prefill_reference_cannot_access_foreign_or_open_scene():
    master = staff("inherit-prefill-foreign")
    campaign = make_campaign("Visible", owner=master)
    foreign = make_campaign("Hidden", owner=staff("inherit-other"))
    hero = make_player(campaign, "Hero")
    bad = closed(make_scene(foreign, "Secret", participants=[]))
    open_scene = make_scene(campaign, "Open", participants=[hero])
    url = reverse("campaign_scene_create", args=[campaign.pk])
    for source in (bad, open_scene):
        result = session(master).get(f"{url}?from_scene={source.pk}")
        assert result.status_code == 404
        assert "Secret" not in result.content.decode()


@pytest.mark.django_db
def test_edit_cannot_self_link_or_use_successor_as_predecessor():
    master = staff("inherit-cycle")
    campaign = make_campaign(owner=master)
    player = make_player(campaign, "Pilot")
    origin = make_scene(campaign, "Origin", participants=[player], mode=TurnMode.MANUAL)
    child = closed(make_scene(campaign, "Later", participants=[player]))
    # Simulate a historical invalid graph created outside workbench validation:
    child.previous_scenes.add(origin)
    client = session(master)
    url = reverse("campaign_scene_edit", args=[campaign.pk, origin.pk])

    for source in (origin, child):
        result = client.post(
            url, scene_data("Corrupted", participants=[player], previous=[source])
        )
        assert result.status_code == 200
        assert "previous_scenes" in result.context["form"].errors
    origin.refresh_from_db()
    assert origin.name == "Origin"
    assert not origin.previous_scenes.exists()


@pytest.mark.django_db
def test_predecessors_locked_after_a_turn_but_metadata_remains_editable():
    master = staff("inherit-locked")
    campaign = make_campaign(owner=master)
    player = make_player(campaign, "Pilot")
    source = closed(make_scene(campaign, "Source", participants=[player]))
    other = closed(make_scene(campaign, "Other", participants=[player]))
    current = make_scene(campaign, "Current", participants=[player], mode=TurnMode.MANUAL)
    current.previous_scenes.add(source)
    Turn.objects.create(scene=current, mode=TurnMode.MANUAL)
    client = session(master)
    url = reverse("campaign_scene_edit", args=[campaign.pk, current.pk])

    rejected = client.post(
        url, scene_data("Trying", participants=[player], previous=[other])
    )
    assert rejected.status_code == 200
    assert "previous_scenes" in rejected.context["form"].errors
    current.refresh_from_db()
    assert current.name == "Current"
    assert list(current.previous_scenes.all()) == [source]

    allowed = client.post(
        url, scene_data("Updated info", participants=[player], previous=[source])
    )
    assert allowed.status_code == 302
    current.refresh_from_db()
    assert current.name == "Updated info"
    assert list(current.previous_scenes.all()) == [source]


@pytest.mark.django_db
def test_predecessors_locked_when_another_scene_depends_on_current():
    master = staff("inherit-dependent")
    campaign = make_campaign(owner=master)
    player = make_player(campaign, "Pilot")
    source = closed(make_scene(campaign, "Source", participants=[player]))
    current = make_scene(campaign, "Current", participants=[player], mode=TurnMode.MANUAL)
    later = make_scene(campaign, "Later", participants=[player])
    later.previous_scenes.add(current)
    url = reverse("campaign_scene_edit", args=[campaign.pk, current.pk])

    result = session(master).post(
        url, scene_data("Invalid", participants=[player], previous=[source])
    )
    assert result.status_code == 200
    assert "previous_scenes" in result.context["form"].errors
    current.refresh_from_db()
    assert not current.previous_scenes.exists()


@pytest.mark.django_db
def test_campaign_dashboard_displays_lineage_and_continue_action():
    master = staff("inherit-ui")
    campaign = make_campaign(owner=master)
    player = make_player(campaign, "Pilot")
    source = closed(make_scene(campaign, "Chapter one", participants=[player]))
    current = make_scene(campaign, "Chapter two", participants=[player])
    current.previous_scenes.add(source)
    html = session(master).get(
        reverse("campaign_detail", args=[campaign.pk])
    ).content.decode()
    assert "Inherits:" in html
    assert "Chapter one" in html
    assert f"?from_scene={source.pk}" in html


@pytest.mark.django_db
def test_editor_does_not_copy_source_summary_or_message_rows():
    master = staff("inherit-no-copy")
    campaign = make_campaign(owner=master)
    player = make_player(campaign, "Pilot")
    source = make_scene(
        campaign, "Source", participants=[player],
        memory_summary="SOURCE-SUMMARY-SENTINEL",
    )
    original = write(campaign, source, "SOURCE-HISTORY-SENTINEL")
    closed(source)
    response = session(master).post(
        reverse("campaign_scene_create", args=[campaign.pk]),
        scene_data("Continuation", participants=[player], previous=[source]),
    )
    assert response.status_code == 302
    current = Scene.objects.get(campaign=campaign, name="Continuation")
    assert current.memory_summary == ""
    assert not current.messages.exists()
    assert current.previous_scenes.filter(pk=source.pk).exists()
    assert Message.objects.filter(pk=original.pk, scene=source).exists()
    context = build_player_context(player=player, scene=current)
    assert "SOURCE-SUMMARY-SENTINEL" in context.system_prompt
    assert any("SOURCE-HISTORY-SENTINEL" in m["content"] for m in context.messages)


@pytest.mark.django_db
def test_unauthorized_users_cannot_create_inherited_scenes():
    owner = staff("inherit-owner")
    other = staff("inherit-outsider")
    campaign = make_campaign(owner=owner)
    player = make_player(campaign, "Pilot")
    source = closed(make_scene(campaign, "Private ancestor", participants=[player]))
    url = reverse("campaign_scene_create", args=[campaign.pk])
    assert session(other).get(url).status_code == 404
    assert session(other).post(
        url, scene_data("Stolen", participants=[player], previous=[source])
    ).status_code == 404
    assert Client().post(
        url, scene_data("Anonymous", participants=[player], previous=[source])
    ).status_code == 302
    assert not campaign.scenes.filter(name__in=["Stolen", "Anonymous"]).exists()
