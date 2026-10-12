"""The Game Master's private campaign clock and conditional NPC calendar."""

from datetime import date, time

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from rpg.models import (
    AuthorType, Campaign, CampaignPlan, CampaignPlanKind,
    CampaignPlanStatus, Message, PlayerTransport, TurnMode, Visibility,
)
from rpg.services.context_builder import build_player_context
from rpg.tests.factories import make_campaign, make_player, make_scene


def gm(name):
    return get_user_model().objects.create_user(
        username=name, password="test-password-very-long", is_staff=True,
    )


def signed_in(user, **kwargs):
    client = Client(**kwargs)
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client


def entry_data(**kw):
    return {
        "kind": "NPC", "title": "The governor departs",
        "npc_name": "Governor",
        "scheduled_date": "2022-10-12",
        "scheduled_time": "14:30",
        "end_date": "",
        "end_time": "",
        "description": "Leaves the capital unless the party intervenes",
        "planned_outcome": "The convoy arrives before midnight",
        "status": "PLANNED",
        "actual_outcome": "",
        **kw,
    }


def add_entry(campaign, title="Private forecast", **kw):
    return CampaignPlan.objects.create(
        campaign=campaign, title=title, kind=CampaignPlanKind.NPC,
        scheduled_date=date(2022, 10, 12), scheduled_time=time(14, 30),
        planned_outcome="The messenger dies unless intercepted.",
        **kw,
    )


@pytest.mark.django_db
def test_calendar_is_linked_from_campaign_and_scene_but_not_player_client():
    user = gm("calendar-link")
    camp = make_campaign(owner=user)
    human = make_player(camp, "Human", transport=PlayerTransport.HUMAN)
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[human])
    token = scene.scene_participants.get(player=human).human_access_token
    url = reverse("campaign_timeline", args=[camp.pk])
    client = signed_in(user)

    assert url in client.get(reverse("campaign_detail", args=[camp.pk])).content.decode()
    assert url in client.get(reverse("scene", args=[scene.pk])).content.decode()
    assert client.get(url).status_code == 200
    human_page = Client().get(reverse("human_player_client", args=[token])).content.decode()
    assert url not in human_page


@pytest.mark.django_db
def test_clock_is_manually_set_isolated_by_campaign_and_never_auto_executes_plans():
    user = gm("calendar-clock")
    one = make_campaign("One", owner=user)
    two = make_campaign("Two", owner=user)
    entry = add_entry(one)
    client = signed_in(user)

    clock = reverse("campaign_clock_update", args=[one.pk])
    assert client.get(clock).status_code == 405
    response = client.post(clock, {
        "world_date": "2022-10-14", "world_time": "21:15",
    })
    assert response.status_code == 302
    one.refresh_from_db()
    two.refresh_from_db()
    entry.refresh_from_db()
    assert one.world_date == date(2022, 10, 14)
    assert one.world_time == time(21, 15)
    assert two.world_date is None
    assert two.world_time is None
    assert entry.status == CampaignPlanStatus.PLANNED

    board = client.get(reverse("campaign_timeline", args=[one.pk]))
    assert "14 Oct 2022" in board.content.decode()
    assert "21:15" in board.content.decode()


@pytest.mark.django_db
def test_invalid_clock_data_cannot_advance_time():
    user = gm("calendar-invalid-clock")
    camp = make_campaign(owner=user)
    clock = reverse("campaign_clock_update", args=[camp.pk])
    client = signed_in(user)

    for post in (
        {"world_date": "not-a-date", "world_time": "12:00"},
        {"world_date": "2022-10-12", "world_time": "25:80"},
        {},
    ):
        result = client.post(clock, post)
        assert result.status_code == 400
        assert result.context["clock_form"].errors
        camp.refresh_from_db()
        assert camp.world_date is None and camp.world_time is None


@pytest.mark.django_db
def test_create_npc_plan_and_markers_then_manually_change_outcome():
    user = gm("calendar-plan")
    camp = make_campaign(owner=user)
    client = signed_in(user)
    create = reverse("campaign_plan_create", args=[camp.pk])

    page = client.get(create + "?on=2022-10-12&kind=NPC")
    assert page.status_code == 200
    assert page.context["form"]["scheduled_date"].value() == date(2022, 10, 12)

    result = client.post(create, entry_data())
    assert result.status_code == 302
    plan = camp.timeline_plans.get()
    assert plan.npc_name == "Governor"
    assert plan.planned_outcome.startswith("The convoy")
    assert plan.status == CampaignPlanStatus.PLANNED

    edit = reverse("campaign_plan_edit", args=[camp.pk, plan.pk])
    result = client.post(edit, entry_data(
        status="CHANGED", actual_outcome="Players stopped the convoy.",
    ))
    assert result.status_code == 302
    plan.refresh_from_db()
    assert plan.status == CampaignPlanStatus.CHANGED
    assert plan.actual_outcome == "Players stopped the convoy."
    assert plan.planned_outcome == "The convoy arrives before midnight"

    marker = client.post(create, entry_data(
        kind="MARKER", title="Eclipse", npc_name="",
        scheduled_time="", description="", planned_outcome="",
        status="COMPLETED",
    ))
    assert marker.status_code == 302
    assert camp.timeline_plans.filter(kind=CampaignPlanKind.MARKER).count() == 1


@pytest.mark.django_db
def test_month_week_day_navigation_and_fictional_clock_highlight():
    user = gm("calendar-grid")
    camp = make_campaign(
        owner=user, world_date=date(2022, 10, 12), world_time=time(9, 45),
    )
    first = add_entry(camp, title="Two-day journey", end_date=date(2022, 10, 13))
    far = CampaignPlan.objects.create(
        campaign=camp, title="Earlier",
        scheduled_date=date(2022, 9, 20),
    )
    url = reverse("campaign_timeline", args=[camp.pk])
    client = signed_in(user)

    month = client.get(url + "?view=month&on=2022-10-12")
    assert month.status_code == 200
    assert len(month.context["cells"]) in (35, 42)
    assert any(c["is_active"] for c in month.context["cells"])
    assert sum(first in c["events"] for c in month.context["cells"]) == 2
    assert all(far not in c["events"] for c in month.context["cells"])
    assert "?view=month&amp;on=2022-09-12" in month.content.decode()

    week = client.get(url + "?view=week&on=2022-10-12")
    assert len(week.context["cells"]) == 7
    assert week.context["cells"][0]["date"] == date(2022, 10, 10)
    assert sum(first in c["events"] for c in week.context["cells"]) == 2

    day = client.get(url + "?view=day&on=2022-10-13")
    assert len(day.context["cells"]) == 1
    assert first in day.context["cells"][0]["events"]
    assert "Two-day journey" in day.content.decode()

    invalid = client.get(url + "?view=unknown&on=not-a-date")
    assert invalid.status_code == 200
    assert invalid.context["view"] == "month"
    assert invalid.context["anchor"] == camp.world_date


@pytest.mark.django_db
def test_events_spanning_month_boundaries_display_on_both_sides():
    user = gm("calendar-span")
    camp = make_campaign(owner=user)
    p = CampaignPlan.objects.create(
        campaign=camp, title="Seven-day journey",
        scheduled_date=date(2022, 9, 29),
        end_date=date(2022, 10, 5),
    )
    client = signed_in(user)
    url = reverse("campaign_timeline", args=[camp.pk])
    october = client.get(url + "?on=2022-10-10")
    assert p in next(c["events"] for c in october.context["cells"] if c["date"] == date(2022, 10, 3))
    september = client.get(url + "?on=2022-09-10")
    assert p in next(c["events"] for c in september.context["cells"] if c["date"] == date(2022, 9, 30))


@pytest.mark.django_db
def test_invalid_event_dates_and_end_times_are_rejected():
    user = gm("calendar-invalid-plan")
    camp = make_campaign(owner=user)
    client = signed_in(user)
    url = reverse("campaign_plan_create", args=[camp.pk])
    bad = (
        entry_data(end_date="2022-10-11"),
        entry_data(end_time="17:00"),
        entry_data(end_date="2022-10-12", end_time="13:00"),
        entry_data(scheduled_date="bad-input"),
        entry_data(title="  "),
    )
    for case in bad:
        response = client.post(url, case)
        assert response.status_code == 200
        assert response.context["form"].errors
        assert camp.timeline_plans.count() == 0


@pytest.mark.django_db
def test_gm_notes_escaped_and_never_auto_added_to_player_context():
    user = gm("calendar-no-leak")
    camp = make_campaign(owner=user)
    player = make_player(camp, "Character")
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[player])
    add_entry(
        camp, title="<script>alert(1)</script>",
        description="<img src=x onerror=alert(1)>",
        actual_outcome="SECRET-GM-PLAN-DETAIL",
    )
    client = signed_in(user)
    html = client.get(reverse("campaign_timeline", args=[camp.pk]) + "?on=2022-10-12").content.decode()
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html
    edit = client.get(reverse("campaign_plan_edit", args=[camp.pk, camp.timeline_plans.get().pk])).content.decode()
    assert "&lt;img" in edit
    assert "<img src=x onerror=alert(1)>" not in edit

    context = build_player_context(player=player, scene=scene)
    assert "SECRET-GM-PLAN-DETAIL" not in context.system_prompt
    assert "SECRET-GM-PLAN-DETAIL" not in str(context.messages)
    assert "The messenger dies unless intercepted." not in str(context.messages)


@pytest.mark.django_db
def test_unauthorized_master_cannot_read_or_edit_another_campaign_board_or_event():
    alice = gm("calendar-owner")
    bob = gm("calendar-outsider")
    own = make_campaign(owner=alice)
    orphan = Campaign.objects.create(name="No owner")
    plan = add_entry(own, title="Secret pact")
    client = signed_in(bob)
    for url in (
        reverse("campaign_timeline", args=[own.pk]),
        reverse("campaign_clock_update", args=[own.pk]),
        reverse("campaign_plan_create", args=[own.pk]),
        reverse("campaign_plan_edit", args=[own.pk, plan.pk]),
        reverse("campaign_timeline", args=[orphan.pk]),
    ):
        assert client.get(url).status_code in (404, 405)
        assert client.post(url, entry_data()).status_code in (404, 405)
    assert client.post(
        reverse("campaign_clock_update", args=[own.pk]),
        {"world_date": "2022-10-15", "world_time": "09:00"},
    ).status_code == 404
    own.refresh_from_db()
    plan.refresh_from_db()
    assert own.world_date is None
    assert plan.title == "Secret pact"

    second = make_campaign(owner=bob)
    assert client.get(reverse("campaign_plan_edit", args=[second.pk, plan.pk])).status_code == 404


@pytest.mark.django_db
def test_anonymous_nonstaff_public_host_and_csrf_cannot_read_or_write_timeline():
    master = gm("calendar-perms")
    camp = make_campaign(owner=master)
    board = reverse("campaign_timeline", args=[camp.pk])
    create = reverse("campaign_plan_create", args=[camp.pk])
    clock = reverse("campaign_clock_update", args=[camp.pk])

    anonymous = Client()
    assert anonymous.get(board).status_code == 302
    assert anonymous.post(create, entry_data()).status_code == 302
    assert anonymous.post(clock, {"world_date": "2022-10-12", "world_time": "12:00"}).status_code == 302

    regular = get_user_model().objects.create_user("calendar-regular", password="secure")
    assert signed_in(regular).get(board).status_code == 403
    assert signed_in(regular).post(create, entry_data()).status_code == 403

    csrf_client = signed_in(master, enforce_csrf_checks=True)
    assert csrf_client.post(create, entry_data()).status_code == 403
    assert csrf_client.get(board).status_code == 200
    assert csrf_client.post(create, entry_data(), HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value).status_code == 302

    with override_settings(
        PUBLIC_PLAYER_HOST="play.example.test",
        ALLOWED_HOSTS=["testserver", "play.example.test"],
    ):
        public = signed_in(master, HTTP_HOST="play.example.test")
        assert public.get(board).status_code == 404
        assert public.post(create, entry_data()).status_code == 404
        assert public.post(clock, {"world_date": "2022-10-12", "world_time": "15:00"}).status_code == 404


@pytest.mark.django_db
def test_admin_plan_list_scoped_to_owner():
    alice = gm("calendar-admin-alice")
    bob = gm("calendar-admin-bob")
    own = make_campaign(owner=alice)
    other = make_campaign(owner=bob)
    a = add_entry(own, title="Alice note")
    b = add_entry(other, title="Bob note")

    admin_client = signed_in(alice)
    response = admin_client.get(reverse("admin:rpg_campaignplan_changelist"))
    assert response.status_code == 200
    assert "Alice note" in response.content.decode()
    assert "Bob note" not in response.content.decode()
    assert admin_client.get(reverse("admin:rpg_campaignplan_change", args=[b.pk])).status_code in (302, 403, 404)
    assert admin_client.get(reverse("admin:rpg_campaignplan_change", args=[a.pk])).status_code == 200

    request = RequestFactory().get("/admin/rpg/campaignplan/")
    request.user = alice
    model_admin = admin.site._registry[CampaignPlan]
    assert list(model_admin.get_queryset(request)) == [a]
