"""Owner-managed co-Game Masters: share access with existing staff accounts.

No external invitation links, automatic account provisioning, or ownership
transfer. Membership is effective immediately and can be revoked immediately.
"""
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from rpg.campaign_access import can_manage_team, visible_campaigns
from rpg.models import Campaign, CampaignCollaborator


class AddCoMasterForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        label="Existing Game Master username",
        widget=forms.TextInput(attrs={
            "autocomplete": "off", "placeholder": "Username",
        }),
        help_text="The user must already have an active staff account.",
    )

    def __init__(self, *args, campaign, **kwargs):
        super().__init__(*args, **kwargs)
        self.campaign = campaign

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        # No creation of users and no invitation to ordinary player accounts.
        user = get_user_model()._default_manager.filter(
            username=username, is_active=True, is_staff=True
        ).first()
        if user is None:
            raise forms.ValidationError("No active Game Master with this username.")
        if self.campaign.owner_id == user.pk:
            raise forms.ValidationError("This user already owns the campaign.")
        if CampaignCollaborator.objects.filter(
            campaign=self.campaign, user=user
        ).exists():
            raise forms.ValidationError("This Game Master already has access.")
        self.cleaned_data["new_member_user"] = user
        return username


def _shared_campaign(request, campaign_id):
    return get_object_or_404(
        visible_campaigns(request.user).select_related("owner"),
        pk=campaign_id,
    )


@require_GET
def team_members(request, campaign_id):
    campaign = _shared_campaign(request, campaign_id)
    return render(request, "rpg/campaign_team.html", {
        "campaign": campaign,
        "members": campaign.collaborators.select_related("user").all(),
        "can_manage": can_manage_team(request.user, campaign),
        "form": AddCoMasterForm(campaign=campaign),
    })


@require_POST
def team_add(request, campaign_id):
    campaign = _shared_campaign(request, campaign_id)
    if not can_manage_team(request.user, campaign):
        raise Http404
    form = AddCoMasterForm(request.POST, campaign=campaign)
    if not form.is_valid():
        return render(request, "rpg/campaign_team.html", {
            "campaign": campaign,
            "members": campaign.collaborators.select_related("user").all(),
            "can_manage": True,
            "form": form,
        }, status=400)

    with transaction.atomic():
        campaign = get_object_or_404(
            Campaign.objects.select_for_update(), pk=campaign_id
        )
        if not can_manage_team(request.user, campaign) or (
            campaign.archived_at or campaign.deleted_at
        ):
            raise Http404
        new_user = form.cleaned_data["new_member_user"]
        if not new_user.is_active or not new_user.is_staff or (
            campaign.owner_id == new_user.pk
        ):
            raise Http404
        try:
            _, created = CampaignCollaborator.objects.get_or_create(
                campaign=campaign, user=new_user
            )
        except IntegrityError:
            created = False
    if not created:
        form.add_error("username", "This Game Master already has access.")
        return render(request, "rpg/campaign_team.html", {
            "campaign": campaign,
            "members": campaign.collaborators.select_related("user").all(),
            "can_manage": True,
            "form": form,
        }, status=400)

    messages.success(request, f"Game Master {new_user.username} added to this campaign.")
    return redirect("campaign_team", campaign_id=campaign.pk)


@require_POST
def team_remove(request, campaign_id, user_id):
    campaign = _shared_campaign(request, campaign_id)
    if not can_manage_team(request.user, campaign):
        raise Http404
    with transaction.atomic():
        campaign = get_object_or_404(
            Campaign.objects.select_for_update(), pk=campaign_id
        )
        if not can_manage_team(request.user, campaign) or (
            campaign.archived_at or campaign.deleted_at
        ):
            raise Http404
        membership = get_object_or_404(
            CampaignCollaborator, campaign=campaign, user_id=user_id,
        )
        username = membership.user.username
        membership.delete()

    messages.success(request, f"Game Master {username} no longer has access.")
    return redirect("campaign_team", campaign_id=campaign.pk)
