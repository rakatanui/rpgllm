"""Small in-workbench fictional clock and conditional one-hour event reminders.

These are GM-only read/write endpoints. Fictional time advances only by an
explicit GM action; warnings refer to that fictional clock, not wall time.
"""
from datetime import datetime, timedelta

from django.contrib import messages
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from rpg.campaign_timeline import CampaignClockForm
from rpg.campaign_workbench import _campaign_for_master
from rpg.models import Campaign, CampaignPlanStatus, Scene

# Deliberately limited controls: large jumps must use the exact date/time form.
ALLOWED_STEPS = {-15, 5, 15, 60}
ALERT_LIMIT = 6


def next_hour_plans(campaign):
    """Return (plan, remaining_minutes) for plans starting within 60 story minutes.

    A plan with no scheduled time has no defined one-hour reminder; events that
    were completed, changed or cancelled no longer generate warnings.
    """
    if not campaign.world_date or not campaign.world_time:
        return []
    now = datetime.combine(campaign.world_date, campaign.world_time)
    try:
        deadline = now + timedelta(hours=1)
    except OverflowError:
        deadline = datetime.max

    candidates = campaign.timeline_plans.filter(
        status=CampaignPlanStatus.PLANNED,
        scheduled_date__range=(now.date(), deadline.date()),
        scheduled_time__isnull=False,
    ).order_by("scheduled_date", "scheduled_time", "pk")
    upcoming = []
    for item in candidates:
        remaining = datetime.combine(
            item.scheduled_date, item.scheduled_time,
        ) - now
        if timedelta(0) <= remaining <= timedelta(hours=1):
            upcoming.append({
                "plan": item,
                "minutes_remaining": int(remaining.total_seconds() // 60),
            })
    return upcoming


def clock_widget_context(campaign, *, scene=None, clock_form=None, error=None):
    return {
        "campaign": campaign,
        "return_scene_id": scene.pk if scene is not None else None,
        "clock_form": clock_form or CampaignClockForm(initial={
            "world_date": campaign.world_date,
            "world_time": campaign.world_time,
        }),
        "clock_error": error,
    }


def _return_url(campaign, posted_scene_id):
    if posted_scene_id:
        scene = Scene.objects.filter(
            campaign_id=campaign.pk, pk=posted_scene_id,
        ).first()
        if scene:
            return reverse("scene", args=[scene.pk])
    return reverse("campaign_detail", args=[campaign.pk])


@require_http_methods(["GET", "POST"])
def campaign_clock_quick(request, campaign_id):
    campaign = _campaign_for_master(request, campaign_id)

    if request.method == "GET":
        return render(
            request, "rpg/_campaign_clock_widget.html",
            clock_widget_context(campaign),
        )

    step_raw = request.POST.get("adjust_minutes")
    form = None
    error = None
    changed = False

    with transaction.atomic():
        # Protect co-masters making independent increments at about the same
        # time from losing one of the changes.
        campaign = get_object_or_404(
            Campaign.objects.select_for_update(),
            pk=campaign.pk, deleted_at__isnull=True, archived_at__isnull=True,
        )
        if step_raw is not None:
            try:
                step = int(step_raw)
            except (TypeError, ValueError):
                step = None
            if step not in ALLOWED_STEPS:
                error = "Unsupported time adjustment."
            elif not campaign.world_date or not campaign.world_time:
                error = "Set the campaign date and time before moving the clock."
            else:
                start = datetime.combine(
                    campaign.world_date, campaign.world_time,
                )
                try:
                    new_time = start + timedelta(minutes=step)
                    campaign.world_date = new_time.date()
                    campaign.world_time = new_time.time()
                    changed = True
                except OverflowError:
                    error = "The resulting campaign date is out of range."
        else:
            form = CampaignClockForm(request.POST)
            if form.is_valid():
                campaign.world_date = form.cleaned_data["world_date"]
                campaign.world_time = form.cleaned_data["world_time"]
                changed = True
            else:
                error = "Enter a valid campaign date and time."

        if changed:
            campaign.save(update_fields=["world_date", "world_time", "updated_at"])

    if request.headers.get("HX-Request") == "true":
        response = render(
            request, "rpg/_campaign_clock_widget.html",
            clock_widget_context(
                campaign, scene=Scene.objects.filter(
                    campaign=campaign, pk=request.POST.get("return_scene_id")
                ).first() if request.POST.get("return_scene_id", "").isdigit() else None,
                clock_form=form if form is not None and not form.is_valid() else None,
                error=error,
            ),
        )
        if changed:
            response["HX-Trigger"] = "campaignClockUpdated"
        return response

    if error:
        messages.error(request, error)
    else:
        messages.success(request, "Campaign time updated; events were not executed.")
    return redirect(_return_url(campaign, request.POST.get("return_scene_id")))


@require_GET
def campaign_clock_alerts(request, campaign_id):
    campaign = _campaign_for_master(request, campaign_id)
    alerts = next_hour_plans(campaign)
    return render(request, "rpg/_campaign_clock_alerts.html", {
        "campaign": campaign,
        "clock_alerts": alerts[:ALERT_LIMIT],
        "extra_alerts": max(0, len(alerts) - ALERT_LIMIT),
    })
