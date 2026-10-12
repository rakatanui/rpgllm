"""A private, campaign-local calendar of conditional NPC plans.

The calendar is GM-only. It intentionally does not feed prompts, advance
automatically, or execute planned actions when fictional time passes.
"""
import calendar
from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from rpg.campaign_workbench import _campaign_for_master
from rpg.models import Campaign, CampaignPlan, CampaignPlanKind


class CampaignClockForm(forms.Form):
    world_date = forms.DateField(
        label="Campaign date",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    world_time = forms.TimeField(
        label="Campaign time",
        input_formats=["%H:%M", "%H:%M:%S"],
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
    )


class CampaignPlanForm(forms.ModelForm):
    class Meta:
        model = CampaignPlan
        fields = (
            "kind", "title", "npc_name", "scheduled_date", "scheduled_time",
            "end_date", "end_time", "description", "planned_outcome",
            "status", "actual_outcome",
        )
        labels = {
            "kind": "Entry type",
            "npc_name": "NPC / faction",
            "scheduled_date": "Start date (in the campaign)",
            "scheduled_time": "Start time",
            "end_date": "End date (optional)",
            "end_time": "End time (optional)",
            "description": "Details / motivation",
            "planned_outcome": "Expected outcome if players do not intervene",
            "status": "What happened to this plan?",
            "actual_outcome": "Consequences / reason for the change",
        }
        widgets = {
            "scheduled_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "scheduled_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
            "end_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "end_time": forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
            "description": forms.Textarea(attrs={"rows": 4}),
            "planned_outcome": forms.Textarea(attrs={"rows": 4}),
            "actual_outcome": forms.Textarea(attrs={"rows": 4}),
        }


def _view_date(raw, default):
    try:
        candidate = parse_date(raw) if raw else None
    except ValueError:
        candidate = None
    # calendar.monthdatescalendar can overflow Python's datetime range.
    if candidate and 2 <= candidate.year <= 9998:
        return candidate
    return default


def _nearby_date(value, *, days=0, months=0):
    try:
        if months:
            count = value.year * 12 + value.month - 1 + months
            year, index = divmod(count, 12)
            return date(year, index + 1, min(value.day, calendar.monthrange(year, index + 1)[1]))
        return value + timedelta(days=days)
    except (OverflowError, ValueError):
        return value


def _calendar_cells(campaign, *, view, anchor):
    if view == "month":
        weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(
            anchor.year, anchor.month,
        )
        days = [day for week in weeks for day in week]
        heading = anchor.strftime("%B %Y")
        previous = _nearby_date(anchor, months=-1)
        following = _nearby_date(anchor, months=1)
    elif view == "week":
        first = anchor - timedelta(days=anchor.weekday())
        days = [first + timedelta(days=offset) for offset in range(7)]
        heading = f"{days[0]:%d %b %Y} – {days[-1]:%d %b %Y}"
        previous = _nearby_date(anchor, days=-7)
        following = _nearby_date(anchor, days=7)
    else:
        days = [anchor]
        heading = anchor.strftime("%A, %d %B %Y")
        previous = _nearby_date(anchor, days=-1)
        following = _nearby_date(anchor, days=1)

    min_date, max_date = days[0], days[-1]
    events = list(campaign.timeline_plans.filter(
        scheduled_date__lte=max_date,
    ).filter(
        Q(end_date__gte=min_date)
        | Q(end_date__isnull=True, scheduled_date__gte=min_date)
    ).order_by("scheduled_date", "scheduled_time", "pk"))

    # Show multi-day plans on each day they occupy; never copy database rows.
    cells = []
    for day in days:
        day_plans = [
            event for event in events
            if event.scheduled_date <= day <= (event.end_date or event.scheduled_date)
        ]
        cells.append({
            "date": day,
            "iso": day.isoformat(),
            "day_number": day.day,
            "is_active": day == campaign.world_date,
            "is_outside": view == "month" and day.month != anchor.month,
            "events": day_plans,
        })
    return cells, heading, previous, following


@require_GET
def campaign_timeline(request, campaign_id):
    campaign = _campaign_for_master(request, campaign_id)
    view = request.GET.get("view", "month")
    if view not in ("month", "week", "day"):
        view = "month"
    anchor = _view_date(request.GET.get("on"), campaign.world_date or date.today())
    cells, heading, previous, following = _calendar_cells(
        campaign, view=view, anchor=anchor,
    )
    upcoming = campaign.timeline_plans.filter(
        scheduled_date__gte=campaign.world_date or anchor,
        status="PLANNED",
    ).order_by("scheduled_date", "scheduled_time", "pk")[:8]
    return render(request, "rpg/campaign_timeline.html", {
        "campaign": campaign,
        "clock_form": CampaignClockForm(initial={
            "world_date": campaign.world_date,
            "world_time": campaign.world_time,
        }),
        "view": view, "anchor": anchor, "heading": heading,
        "previous": previous, "following": following,
        "cells": cells, "upcoming": upcoming,
        "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    })


@require_POST
def campaign_clock_update(request, campaign_id):
    campaign = _campaign_for_master(request, campaign_id)
    form = CampaignClockForm(request.POST)
    if not form.is_valid():
        return render(request, "rpg/campaign_timeline.html", {
            **_timeline_context(campaign, request),
            "clock_form": form,
        }, status=400)
    campaign.world_date = form.cleaned_data["world_date"]
    campaign.world_time = form.cleaned_data["world_time"]
    campaign.save(update_fields=["world_date", "world_time", "updated_at"])
    messages.success(request, "Campaign clock updated. NPC plans were not executed.")
    return redirect(
        reverse("campaign_timeline", args=[campaign.pk])
        + "?view=day&on=" + campaign.world_date.isoformat()
    )


def _timeline_context(campaign, request):
    """Rebuild the board for an invalid clock submission without losing errors."""
    view = request.GET.get("view", "month")
    if view not in ("month", "week", "day"):
        view = "month"
    anchor = _view_date(request.GET.get("on"), campaign.world_date or date.today())
    cells, heading, previous, following = _calendar_cells(
        campaign, view=view, anchor=anchor,
    )
    return {
        "campaign": campaign, "view": view, "anchor": anchor,
        "heading": heading, "previous": previous, "following": following,
        "cells": cells, "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "upcoming": campaign.timeline_plans.filter(
            scheduled_date__gte=campaign.world_date or anchor,
            status="PLANNED",
        ).order_by("scheduled_date", "scheduled_time", "pk")[:8],
    }


def _plan_editor(request, campaign, plan=None):
    if plan is None:
        initial = {}
        selected = _view_date(
            request.GET.get("on"), campaign.world_date or date.today(),
        )
        initial["scheduled_date"] = selected
        initial["scheduled_time"] = campaign.world_time if selected == campaign.world_date else None
        if request.GET.get("kind") in CampaignPlanKind.values:
            initial["kind"] = request.GET["kind"]
        form = CampaignPlanForm(
            request.POST if request.method == "POST" else None,
            initial=initial,
        )
    else:
        form = CampaignPlanForm(
            request.POST if request.method == "POST" else None,
            instance=plan,
        )

    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.campaign = campaign
        obj.save()
        messages.success(request, f"Calendar entry “{obj.title}” saved.")
        return redirect(
            reverse("campaign_timeline", args=[campaign.pk])
            + "?view=month&on=" + obj.scheduled_date.isoformat()
        )
    return render(request, "rpg/campaign_plan_form.html", {
        "campaign": campaign, "plan": plan, "form": form,
    })


@require_http_methods(["GET", "POST"])
def campaign_plan_create(request, campaign_id):
    campaign = _campaign_for_master(request, campaign_id)
    return _plan_editor(request, campaign)


@require_http_methods(["GET", "POST"])
def campaign_plan_edit(request, campaign_id, plan_id):
    campaign = _campaign_for_master(request, campaign_id)
    plan = get_object_or_404(CampaignPlan, campaign=campaign, pk=plan_id)
    return _plan_editor(request, campaign, plan)
