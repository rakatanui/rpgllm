"""In-workbench campaign clock and one-hour NPC timeline reminders."""

from datetime import date, time

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from rpg.models import (
    CampaignPlan, CampaignPlanKind, CampaignPlanStatus, PlayerTransport, TurnMode,
)
from rpg.tests.factories import make_campaign, make_player, make_scene


def gm(name):
    return get_user_model().objects.create_user(
        username=name, password="a-safe-test-password", is_staff=True,
    )


def logged_in(user, **options):
    client = Client(**options)
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client


def plan(campaign, title, day, at, *, status=CampaignPlanStatus.PLANNED, kind=CampaignPlanKind.NPC):
    return CampaignPlan.objects.create(
        campaign=campaign, title=title, scheduled_date=day,
        scheduled_time=at, status=status, kind=kind,
    )


@pytest.mark.django_db
def test_inline_clock_is_visible_on_gm_scene_and_campaign_workbench_not_public_player_client():
    owner = gm("clock-inline-owner")
    camp = make_campaign(owner=owner, world_date=date(2022, 10, 12), world_time=time(13, 30))
    human = make_player(camp, "Human", transport=PlayerTransport.HUMAN)
    scene = make_scene(camp, mode=TurnMode.MANUAL, participants=[human])
    token = scene.scene_participants.get(player=human).human_access_token
    client = logged_in(owner)
    for url in (reverse("scene", args=[scene.pk]), reverse("campaign_detail", args=[camp.pk])):
        response = client.get(url)
        assert response.status_code == 200
        html = response.content.decode()
        assert 'id="gm-campaign-clock"' in html
        assert 'id="gm-campaign-alerts"' in html
        assert 'name="world_date"' in html
        assert 'name="world_time"' in html
        assert "12.10.2022" in html
        assert "13:30" in html
        assert "every 15s" in html
        assert reverse("campaign_clock_quick", args=[camp.pk]) in html
    public_html = Client().get(reverse("human_player_client", args=[token])).content.decode()
    assert "gm-campaign-clock" not in public_html
    assert "gm-campaign-alerts" not in public_html


@pytest.mark.django_db
def test_one_hour_alert_exact_boundaries_event_links_and_excludes_irrelevant_events():
    owner = gm("clock-boundary-owner")
    camp = make_campaign(owner=owner, world_date=date(2022, 10, 12), world_time=time(10, 0))
    at_now = plan(camp, "Happening now", date(2022, 10, 12), time(10, 0))
    in_hour = plan(camp, "Governor moves", date(2022, 10, 12), time(11, 0))
    too_late = plan(camp, "61 minutes", date(2022, 10, 12), time(11, 1))
    past = plan(camp, "Earlier", date(2022, 10, 12), time(9, 59))
    cancelled = plan(camp, "Cancelled", date(2022, 10, 12), time(10, 35), status="CANCELLED")
    changed = plan(camp, "Changed", date(2022, 10, 12), time(10, 35), status="CHANGED")
    completed = plan(camp, "Completed", date(2022, 10, 12), time(10, 35), status="COMPLETED")
    all_day = plan(camp, "Unspecified time", date(2022, 10, 12), None)
    marker = plan(camp, "Story marker", date(2022, 10, 12), time(10, 20), kind=CampaignPlanKind.MARKER)
    other = make_campaign("Other", owner=owner)
    plan(other, "Foreign future", date(2022, 10, 12), time(10, 30))

    client = logged_in(owner)
    response = client.get(reverse("campaign_clock_alerts", args=[camp.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    assert len(response.context["clock_alerts"]) == 3
    assert "Happening now" in html and "Сейчас" in html
    assert "Governor moves" in html and "Через 60 мин" in html
    assert "Story marker" in html and "Через 20 мин" in html
    for ignored in (too_late, past, cancelled, changed, completed, all_day):
        assert ignored.title not in html
    assert "Foreign future" not in html
    assert reverse("campaign_plan_edit", args=[camp.pk, in_hour.pk]) in html
    assert reverse("campaign_plan_edit", args=[camp.pk, at_now.pk]) in html
    assert reverse("campaign_plan_edit", args=[camp.pk, marker.pk]) in html

    scene = make_scene(camp, "Prologue")
    scene_page = client.get(reverse("scene", args=[scene.pk]))
    assert "Governor moves" in scene_page.content.decode()


@pytest.mark.django_db
def test_reminders_cross_midnight_follow_manual_clock_not_real_wall_time():
    owner = gm("clock-midnight")
    camp = make_campaign(owner=owner, world_date=date(2022, 10, 12), world_time=time(23, 30))
    plan(camp, "Late tonight", date(2022, 10, 12), time(23, 55))
    plan(camp, "Tomorrow at midnight", date(2022, 10, 13), time(0, 0))
    plan(camp, "Tomorrow in one hour", date(2022, 10, 13), time(0, 30))
    plan(camp, "Too late", date(2022, 10, 13), time(0, 31))
    html = logged_in(owner).get(
        reverse("campaign_clock_alerts", args=[camp.pk])
    ).content.decode()
    assert "Late tonight" in html
    assert "Tomorrow at midnight" in html
    assert "Tomorrow in one hour" in html
    assert "Too late" not in html
    assert "Через 60 мин" in html


@pytest.mark.django_db
def test_quick_set_date_and_time_from_scene_and_updates_reminders_with_htmx():
    owner = gm("clock-quick-save")
    camp = make_campaign(owner=owner)
    scene = make_scene(camp)
    event = plan(camp, "The arrival", date(2022, 10, 12), time(15, 0))
    client = logged_in(owner)
    quick = reverse("campaign_clock_quick", args=[camp.pk])
    response = client.post(quick, {
        "world_date": "2022-10-12", "world_time": "14:10",
        "return_scene_id": str(scene.pk),
    }, HTTP_HX_REQUEST="true")
    assert response.status_code == 200
    assert response["HX-Trigger"] == "campaignClockUpdated"
    assert "12.10.2022" in response.content.decode()
    assert "14:10" in response.content.decode()
    assert 'name="return_scene_id"' in response.content.decode()
    camp.refresh_from_db()
    assert (camp.world_date, camp.world_time) == (date(2022, 10, 12), time(14, 10))
    assert "The arrival" in client.get(
        reverse("campaign_clock_alerts", args=[camp.pk])
    ).content.decode()
    event.refresh_from_db()
    assert event.status == CampaignPlanStatus.PLANNED


@pytest.mark.django_db
def test_quick_step_buttons_move_clock_across_day_boundaries_without_running_plans():
    owner = gm("clock-quick-step")
    camp = make_campaign(owner=owner, world_date=date(2022, 12, 31), world_time=time(23, 50))
    event = plan(camp, "New-year plan", date(2023, 1, 1), time(0, 30))
    client = logged_in(owner)
    quick = reverse("campaign_clock_quick", args=[camp.pk])

    for value, expected in (
        ("15", (date(2023, 1, 1), time(0, 5))),
        ("5", (date(2023, 1, 1), time(0, 10))),
        ("60", (date(2023, 1, 1), time(1, 10))),
        ("-15", (date(2023, 1, 1), time(0, 55))),
    ):
        response = client.post(
            quick, {"adjust_minutes": value}, HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        assert response["HX-Trigger"] == "campaignClockUpdated"
        camp.refresh_from_db()
        assert (camp.world_date, camp.world_time) == expected
    event.refresh_from_db()
    assert event.status == CampaignPlanStatus.PLANNED
    assert not camp.scenes.exists()


@pytest.mark.django_db
def test_quick_save_without_htmx_returns_to_same_scene_or_campaign():
    owner = gm("clock-no-js")
    camp = make_campaign(owner=owner)
    scene = make_scene(camp)
    response = logged_in(owner).post(
        reverse("campaign_clock_quick", args=[camp.pk]),
        {"world_date": "2022-10-12", "world_time": "22:45", "return_scene_id": scene.pk},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("scene", args=[scene.pk])
    response = logged_in(owner).post(
        reverse("campaign_clock_quick", args=[camp.pk]),
        {"adjust_minutes": "-15"},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("campaign_detail", args=[camp.pk])
    camp.refresh_from_db()
    assert camp.world_time == time(22, 30)


@pytest.mark.django_db
def test_quick_form_rejects_invalid_and_unset_dates_without_mutation():
    owner = gm("clock-invalid")
    camp = make_campaign(owner=owner)
    client = logged_in(owner)
    quick = reverse("campaign_clock_quick", args=[camp.pk])
    for value in ("999", "x", "", "-100"):
        response = client.post(
            quick, {"adjust_minutes": value}, HTTP_HX_REQUEST="true",
        )
        assert response.status_code == 200
        assert 'role="alert"' in response.content.decode()
        assert "HX-Trigger" not in response
        camp.refresh_from_db()
        assert camp.world_date is None
    response = client.post(
        quick, {"world_date": "not-a-date", "world_time": "99:59"},
        HTTP_HX_REQUEST="true",
    )
    assert response.status_code == 200
    assert 'role="alert"' in response.content.decode()
    camp.refresh_from_db()
    assert camp.world_date is None and camp.world_time is None


@pytest.mark.django_db
def test_alerts_require_clock_and_only_show_six_plans_with_overflow_link():
    owner = gm("clock-many")
    camp = make_campaign(owner=owner)
    for index in range(9):
        plan(camp, f"Story plan {index}", date(2022, 10, 12), time(12, index))
    client = logged_in(owner)
    url = reverse("campaign_clock_alerts", args=[camp.pk])
    assert "Story plan" not in client.get(url).content.decode()
    camp.world_date = date(2022, 10, 12)
    camp.world_time = time(12, 0)
    camp.save(update_fields=["world_date", "world_time"])
    response = client.get(url)
    assert len(response.context["clock_alerts"]) == 6
    assert response.context["extra_alerts"] == 3
    assert "Ещё событий: 3" in response.content.decode()
    assert "Story plan 8" not in response.content.decode()


@pytest.mark.django_db
def test_quick_clock_is_scoped_to_owner_or_co_master_and_excludes_foreign_campaigns():
    from rpg.models import CampaignCollaborator

    owner, co, stranger = gm("clock-perms-owner"), gm("clock-perms-co"), gm("clock-perms-stranger")
    camp = make_campaign(owner=owner)
    CampaignCollaborator.objects.create(campaign=camp, user=co)
    other = make_campaign("Other", owner=stranger)
    client = logged_in(co)
    assert client.get(reverse("campaign_clock_alerts", args=[camp.pk])).status_code == 200
    assert client.post(reverse("campaign_clock_quick", args=[camp.pk]), {
        "world_date": "2022-10-12", "world_time": "15:00",
    }).status_code == 302
    assert client.get(reverse("campaign_clock_alerts", args=[other.pk])).status_code == 404
    assert client.post(reverse("campaign_clock_quick", args=[other.pk]), {
        "world_date": "2022-10-12", "world_time": "15:00",
    }).status_code == 404
    other.refresh_from_db()
    assert other.world_date is None

    CampaignCollaborator.objects.filter(campaign=camp, user=co).delete()
    assert client.get(reverse("campaign_clock_alerts", args=[camp.pk])).status_code == 404
    assert client.post(reverse("campaign_clock_quick", args=[camp.pk]), {
        "adjust_minutes": "60",
    }).status_code == 404


@pytest.mark.django_db
def test_archived_campaign_clock_remains_readable_but_not_editable():
    from django.utils import timezone

    owner = gm("clock-archived")
    camp = make_campaign(
        owner=owner, world_date=date(2022, 10, 12), world_time=time(10, 0),
        archived_at=timezone.now(),
    )
    client = logged_in(owner)
    detail = client.get(reverse("campaign_detail", args=[camp.pk]))
    assert detail.status_code == 200
    assert "Архив: изменение времени недоступно" in detail.content.decode()
    assert "quick_world_date" not in detail.content.decode()
    assert client.get(reverse("campaign_clock_alerts", args=[camp.pk])).status_code == 200
    assert client.post(reverse("campaign_clock_quick", args=[camp.pk]), {
        "adjust_minutes": "60",
    }).status_code == 409
    camp.refresh_from_db()
    assert camp.world_time == time(10, 0)


@pytest.mark.django_db
@override_settings(
    PUBLIC_PLAYER_HOST="play.example.test",
    ALLOWED_HOSTS=["testserver", "play.example.test"],
)
def test_quick_clock_and_alert_urls_not_available_on_public_player_host():
    owner = gm("clock-public")
    camp = make_campaign(owner=owner)
    client = logged_in(owner, HTTP_HOST="play.example.test")
    assert client.get(reverse("campaign_clock_alerts", args=[camp.pk])).status_code == 404
    assert client.post(reverse("campaign_clock_quick", args=[camp.pk]), {
        "adjust_minutes": "60",
    }).status_code == 404


@pytest.mark.django_db
def test_csrf_anonymous_and_non_staff_clock_restrictions():
    owner = gm("clock-csrf")
    camp = make_campaign(owner=owner)
    quick = reverse("campaign_clock_quick", args=[camp.pk])
    for method in ("get", "post"):
        anon = Client()
        result = getattr(anon, method)(quick)
        assert result.status_code == 302
    ordinary = get_user_model().objects.create_user(
        username="clock-non-staff", password="long-test-password",
    )
    assert logged_in(ordinary).post(quick, {
        "world_date": "2022-10-12", "world_time": "18:00",
    }).status_code == 403
    strict = logged_in(owner, enforce_csrf_checks=True)
    assert strict.post(quick, {"adjust_minutes": "5"}).status_code == 403
    assert strict.get(quick).status_code == 200
    token = strict.cookies["csrftoken"].value
    assert strict.post(quick, {
        "world_date": "2022-10-12", "world_time": "18:00",
    }, HTTP_X_CSRFTOKEN=token).status_code == 302
