"""Reversible campaign archival and deletion with an explicit superuser purge.

All operations take a row lock on the campaign; never let a background task or
unfinished execution be destroyed by an accidental campaign operation.
"""
import logging

from django.contrib import messages
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from rpg.models import (
    Campaign, CharacterAppearance, ExecutionState, GameMasterExecution,
    GameMasterExecutionState, Player, Scene, Turn, TurnExecution, TurnState,
)

log = logging.getLogger(__name__)

ACTIONS = {
    "archive": {
        "verb": "Archive", "heading": "Archive campaign",
        "explanation": (
            "Freeze gameplay and editing. All scenes, characters, messages, "
            "calendar entries and images remain stored. The GM can still read "
            "campaign history, and restore the campaign later."
        ),
    },
    "unarchive": {
        "verb": "Restore", "heading": "Restore archived campaign",
        "explanation": "Return this archived campaign to active play without resetting its data or player links.",
    },
    "delete": {
        "verb": "Move to bin", "heading": "Move campaign to recycle bin",
        "explanation": (
            "Remove the campaign from playable and archive views, and make its "
            "player links unavailable. All data remains stored until a superuser "
            "explicitly erases it. This can be reversed."
        ),
    },
    "recover": {
        "verb": "Recover", "heading": "Recover deleted campaign",
        "explanation": (
            "Recover the campaign from the recycle bin. If it was archived "
            "before deletion, it will return to the archive rather than active play."
        ),
    },
    "purge": {
        "verb": "Erase forever", "heading": "Permanently erase campaign",
        "explanation": (
            "SUPERUSER ONLY. Destroy the campaign, scenes, messages, turns, "
            "characters, calendar and associated stored portraits. "
            "This cannot be undone. Ensure backups exist before proceeding."
        ),
    },
}
_DESTRUCTIVE = frozenset(("archive", "delete", "purge"))


def _incomplete_work_exists(campaign):
    """Archived or deleted campaigns must not strand live turn/LLM work."""
    scenes = Scene.objects.filter(campaign=campaign)
    if Turn.objects.filter(
        scene__in=scenes, state__in=(TurnState.PENDING, TurnState.RUNNING),
    ).exists():
        return True
    if TurnExecution.objects.filter(
        turn__scene__in=scenes,
        state__in=(
            ExecutionState.PENDING, ExecutionState.RUNNING,
            ExecutionState.WAITING_EXTERNAL, ExecutionState.WAITING_HUMAN,
        ),
    ).exists():
        return True
    if GameMasterExecution.objects.filter(
        scene__in=scenes,
        state__in=(
            GameMasterExecutionState.PENDING,
            GameMasterExecutionState.RUNNING,
            GameMasterExecutionState.WAITING_EXTERNAL,
        ),
    ).exists():
        return True
    return False


def _media_paths(campaign):
    """Media are in Django storage, not inside database cascade graphs."""
    paths = set(
        Player.objects.filter(campaign=campaign)
        .exclude(character_image="")
        .values_list("character_image", flat=True)
    )
    for portrait, fullbody in CharacterAppearance.objects.filter(
        player__campaign=campaign
    ).values_list("portrait_image", "fullbody_image"):
        paths.update(path for path in (portrait, fullbody) if path)
    return {path for path in paths if path}


def _delete_orphaned_media(paths):
    """Delete only files with no remaining DB references, after commit."""
    for path in paths:
        if (
            Player.objects.filter(character_image=path).exists()
            or CharacterAppearance.objects.filter(portrait_image=path).exists()
            or CharacterAppearance.objects.filter(fullbody_image=path).exists()
        ):
            continue
        try:
            default_storage.delete(path)
        except Exception:
            log.exception("Unable to clean up unreferenced campaign media")


@require_http_methods(["GET", "POST"])
def campaign_lifecycle(request, campaign_id, action):
    details = ACTIONS.get(action)
    if details is None:
        raise Http404
    if action == "purge" and not request.user.is_superuser:
        return HttpResponseForbidden("Only a superuser can permanently erase a campaign.")

    queryset = Campaign.objects.all()
    if not request.user.is_superuser:
        queryset = queryset.filter(owner=request.user)
    campaign = get_object_or_404(queryset, pk=campaign_id)

    # Only the right transition is offered; a missing or stale link is not an
    # invitation to move the campaign into some unintended lifecycle state.
    if action == "archive" and (campaign.archived_at or campaign.deleted_at):
        raise Http404
    if action == "unarchive" and (not campaign.archived_at or campaign.deleted_at):
        raise Http404
    if action == "delete" and campaign.deleted_at:
        raise Http404
    if action in ("recover", "purge") and not campaign.deleted_at:
        raise Http404

    if request.method == "GET":
        return render(request, "rpg/campaign_lifecycle.html", {
            "campaign": campaign, "action": action, "details": details,
            "needs_name": action in _DESTRUCTIVE,
        })

    if action in _DESTRUCTIVE:
        if request.POST.get("confirm_name", "") != campaign.name:
            return render(request, "rpg/campaign_lifecycle.html", {
                "campaign": campaign, "action": action, "details": details,
                "needs_name": True,
                "error": "Enter the campaign name exactly as shown.",
            }, status=400)

    with transaction.atomic():
        locked = get_object_or_404(queryset.select_for_update(), pk=campaign_id)

        # Another browser tab might have changed its state after the GET page.
        if action == "archive" and (locked.archived_at or locked.deleted_at):
            return HttpResponseBadRequest("Campaign is no longer active.")
        if action == "unarchive" and (not locked.archived_at or locked.deleted_at):
            return HttpResponseBadRequest("Campaign is no longer archived.")
        if action == "delete" and locked.deleted_at:
            return HttpResponseBadRequest("Campaign is already in the recycle bin.")
        if action in ("recover", "purge") and not locked.deleted_at:
            return HttpResponseBadRequest("Campaign is no longer in the recycle bin.")

        if action in ("archive", "delete", "purge") and _incomplete_work_exists(locked):
            return render(request, "rpg/campaign_lifecycle.html", {
                "campaign": locked, "action": action, "details": details,
                "needs_name": action in _DESTRUCTIVE,
                "error": (
                    "The campaign still has pending, running or waiting game/LLM operations. "
                    "Finish or resolve these operations before changing its status."
                ),
            }, status=409)

        if action == "archive":
            locked.archived_at = timezone.now()
            locked.save(update_fields=["archived_at", "updated_at"])
        elif action == "unarchive":
            locked.archived_at = None
            locked.save(update_fields=["archived_at", "updated_at"])
        elif action == "delete":
            locked.deleted_at = timezone.now()
            locked.save(update_fields=["deleted_at", "updated_at"])
        elif action == "recover":
            locked.deleted_at = None
            locked.save(update_fields=["deleted_at", "updated_at"])
        else:
            paths = _media_paths(locked)
            locked.delete()
            transaction.on_commit(lambda: _delete_orphaned_media(paths))

    messages.success(request, f"Campaign “{campaign.name}”: {details['verb'].lower()} completed.")
    return redirect("campaigns")
