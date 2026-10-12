"""Campaign-specific workbench: player profiles, scenes, and their participants."""

from django import forms
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from rpg.models import Campaign, Player, Scene, SceneParticipant


def _campaign_for_master(request, campaign_id):
    campaigns = Campaign.objects.all()
    if not request.user.is_superuser:
        campaigns = campaigns.filter(owner=request.user)
    return get_object_or_404(campaigns, pk=campaign_id)


class PlayerWorkbenchForm(forms.ModelForm):
    class Meta:
        model = Player
        fields = (
            "display_name", "transport", "character_prompt", "character_summary",
            "characteristics", "abilities", "memory_summary", "model_config",
            "fallback_model_config", "manual_chat_label", "manual_chat_url",
            "manual_chat_context_mode",
        )
        labels = {
            "display_name": "Character / player name",
            "character_prompt": "Character instructions for the model",
            "character_summary": "Character overview",
            "characteristics": "Characteristics",
            "abilities": "Abilities",
            "memory_summary": "Private long-term character memory",
        }
        widgets = {
            "character_prompt": forms.Textarea(attrs={"rows": 6}),
            "character_summary": forms.Textarea(attrs={"rows": 3}),
            "characteristics": forms.Textarea(attrs={"rows": 4}),
            "abilities": forms.Textarea(attrs={"rows": 4}),
            "memory_summary": forms.Textarea(attrs={"rows": 5}),
        }

    def save(self, commit=True):
        player = super().save(commit=False)
        if any(field in self.changed_data for field in (
            "transport", "manual_chat_url", "manual_chat_context_mode"
        )):
            player.manual_chat_initialized = False
        if commit:
            player.save()
            self.save_m2m()
        return player


class SceneWorkbenchForm(forms.ModelForm):
    participants = forms.ModelMultipleChoiceField(
        queryset=Player.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Scene participants",
        help_text="Only characters from this campaign. Order is set in the scene's ROUND controls.",
    )

    class Meta:
        model = Scene
        fields = ("name", "description", "dialogue_language", "mode", "memory_summary")
        labels = {
            "name": "Scene name",
            "description": "Scene setup",
            "dialogue_language": "Dialogue language",
            "mode": "Turn mode",
            "memory_summary": "Scene memory summary",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "memory_summary": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, campaign, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["participants"].queryset = campaign.players.order_by("created_at", "pk")
        if self.instance.pk and not self.is_bound:
            self.initial["participants"] = list(
                self.instance.scene_participants.values_list("player_id", flat=True)
            )

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk or "participants" not in cleaned:
            return cleaned

        previous = set(self.instance.scene_participants.values_list("player_id", flat=True))
        updated = {p.pk for p in cleaned["participants"]}
        if previous != updated and (
            self.instance.is_closed
            or self.instance.turns.exists()
            or self.instance.messages.exists()
            or self.instance.gm_executions.exists()
        ):
            self.add_error(
                "participants",
                "The participant roster is locked after play begins. "
                "Use a follow-up scene to change the cast.",
            )
        return cleaned


@require_http_methods(["GET"])
def campaign_detail(request, campaign_id):
    campaign = _campaign_for_master(request, campaign_id)
    players = list(campaign.players.select_related("model_config").order_by("created_at", "pk"))
    scenes = (
        campaign.scenes
        .prefetch_related("scene_participants__player")
        .order_by("created_at", "pk")
    )
    return render(request, "rpg/campaign_detail.html", {
        "campaign": campaign, "players": players, "scenes": scenes,
    })


def _player_editor(request, campaign, player=None):
    instance = player if player is not None else Player(campaign=campaign)
    form = PlayerWorkbenchForm(
        request.POST if request.method == "POST" else None, instance=instance
    )
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"Character “{saved.display_name}” saved.")
        return redirect("campaign_detail", campaign_id=campaign.pk)
    return render(request, "rpg/campaign_player_form.html", {
        "campaign": campaign, "player": player, "form": form,
    })


@require_http_methods(["GET", "POST"])
def player_create(request, campaign_id):
    return _player_editor(request, _campaign_for_master(request, campaign_id))


@require_http_methods(["GET", "POST"])
def player_edit(request, campaign_id, player_id):
    campaign = _campaign_for_master(request, campaign_id)
    player = get_object_or_404(Player, pk=player_id, campaign=campaign)
    return _player_editor(request, campaign, player)


def _scene_editor(request, campaign, scene=None):
    if scene is not None and scene.is_closed:
        return HttpResponseForbidden("Closed scenes are read-only.")

    instance = scene if scene is not None else Scene(campaign=campaign)
    form = SceneWorkbenchForm(
        request.POST if request.method == "POST" else None,
        instance=instance, campaign=campaign,
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            if scene is not None:
                locked = Scene.objects.select_for_update().get(pk=scene.pk)
                if locked.is_closed:
                    return HttpResponseForbidden("Closed scenes are read-only.")
                current_ids = set(
                    locked.scene_participants.values_list("player_id", flat=True)
                )
                selected_ids = {p.pk for p in form.cleaned_data["participants"]}
                if current_ids != selected_ids and (
                    locked.turns.exists()
                    or locked.messages.exists()
                    or locked.gm_executions.exists()
                ):
                    return HttpResponse(
                        "Participant roster changed while the scene was active. Reload.",
                        status=409,
                    )
                # Keep the form attached to the locked DB instance.
                for field in form.Meta.fields:
                    setattr(locked, field, getattr(form.instance, field))
                saved = locked
            else:
                saved = form.save(commit=False)
                selected_ids = {p.pk for p in form.cleaned_data["participants"]}
                current_ids = set()

            if selected_ids != current_ids:
                # Invalidates any previously confirmed round order. The GM
                # must reconfirm it before running another ROUND turn.
                saved.round_order = []
                saved.active_player_index = 0

            saved.save()

            if selected_ids != current_ids:
                # Retained participants are never deleted: their bearer links,
                # access revocation settings and selected appearances survive.
                if scene is not None:
                    saved.scene_participants.exclude(player_id__in=selected_ids).delete()
                present_ids = set(
                    saved.scene_participants.values_list("player_id", flat=True)
                )
                ordered_ids = list(
                    campaign.players.filter(pk__in=selected_ids)
                    .order_by("created_at", "pk").values_list("pk", flat=True)
                )
                for position, player_id in enumerate(ordered_ids):
                    if player_id not in present_ids:
                        SceneParticipant.objects.create(
                            scene=saved, player_id=player_id, order=position
                        )
                # Normalize order without replacing existing through rows.
                for position, player_id in enumerate(ordered_ids):
                    SceneParticipant.objects.filter(
                        scene=saved, player_id=player_id
                    ).update(order=position)

        messages.success(request, f"Scene “{saved.name}” saved.")
        return redirect("campaign_detail", campaign_id=campaign.pk)

    return render(request, "rpg/campaign_scene_form.html", {
        "campaign": campaign, "scene": scene, "form": form,
    })


@require_http_methods(["GET", "POST"])
def scene_create(request, campaign_id):
    return _scene_editor(request, _campaign_for_master(request, campaign_id))


@require_http_methods(["GET", "POST"])
def scene_edit(request, campaign_id, scene_id):
    campaign = _campaign_for_master(request, campaign_id)
    scene = get_object_or_404(Scene, pk=scene_id, campaign=campaign)
    return _scene_editor(request, campaign, scene)
