"""Campaign-specific workbench: player profiles, scenes, and their participants."""

from django import forms
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from rpg.models import Campaign, Player, Scene, SceneParticipant
from rpg.campaign_access import visible_campaigns
from rpg.services.context_builder import get_scene_lineage


def _campaign_for_master(request, campaign_id):
    return get_object_or_404(visible_campaigns(request.user), pk=campaign_id)


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

    def clean_display_name(self):
        name = self.cleaned_data["display_name"].strip()
        # The form excludes campaign, so validate uniqueness explicitly.
        duplicates = Player.objects.filter(
            campaign_id=self.instance.campaign_id, display_name=name
        )
        if self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        if duplicates.exists():
            raise forms.ValidationError("A character with this name already exists in this campaign.")
        return name

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
    previous_scenes = forms.ModelMultipleChoiceField(
        queryset=Scene.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Inherited scene history",
        help_text=(
            "Choose closed scenes from this campaign. Their history and summaries "
            "remain linked, not copied. Players inherit only scenes they attended "
            "and only messages they were entitled to see."
        ),
    )
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
        predecessors = campaign.scenes.filter(is_closed=True)
        if self.instance.pk:
            predecessors = predecessors.exclude(pk=self.instance.pk)
        self.fields["previous_scenes"].queryset = predecessors.order_by("created_at", "pk")
        if self.instance.pk and not self.is_bound:
            self.initial["participants"] = list(
                self.instance.scene_participants.values_list("player_id", flat=True)
            )
            self.initial["previous_scenes"] = list(
                self.instance.previous_scenes.values_list("pk", flat=True)
            )

    def clean_previous_scenes(self):
        selected = self.cleaned_data["previous_scenes"]
        for predecessor in selected:
            if predecessor.campaign_id != self.instance.campaign_id:
                raise forms.ValidationError("Previous scenes must belong to this campaign.")
            if not predecessor.is_closed:
                raise forms.ValidationError("Only closed scenes can be inherited.")
            if self.instance.pk and any(
                ancestor.pk == self.instance.pk
                for ancestor in get_scene_lineage(predecessor)
            ):
                raise forms.ValidationError("A scene cannot inherit itself or create a cycle.")
        return selected

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk or "participants" not in cleaned:
            return cleaned

        previous = set(self.instance.scene_participants.values_list("player_id", flat=True))
        updated = {p.pk for p in cleaned["participants"]}
        source_ids = set(self.instance.previous_scenes.values_list("pk", flat=True))
        new_source_ids = (
            {source.pk for source in cleaned["previous_scenes"]}
            if "previous_scenes" in cleaned else source_ids
        )
        started = (
            self.instance.is_closed
            or self.instance.turns.exists()
            or self.instance.messages.exists()
            or self.instance.gm_executions.exists()
        )
        if previous != updated and started:
            self.add_error(
                "participants",
                "The participant roster is locked after play begins. "
                "Use a follow-up scene to change the cast.",
            )
        if source_ids != new_source_ids and (
            started or self.instance.next_scenes.exists()
        ):
            self.add_error(
                "previous_scenes",
                "Inherited history is locked after play begins or when another "
                "scene already depends on this one.",
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
    from rpg.campaign_clock import clock_widget_context, next_hour_plans
    alerts = next_hour_plans(campaign)
    return render(request, "rpg/campaign_detail.html", {
        "campaign": campaign, "players": players, "scenes": scenes,
        **clock_widget_context(campaign),
        "clock_alerts": alerts[:6], "extra_alerts": max(0, len(alerts) - 6),
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
    initial = {}
    if scene is None and request.method == "GET" and request.GET.get("from_scene"):
        source = get_object_or_404(
            campaign.scenes.filter(is_closed=True),
            pk=request.GET["from_scene"],
        )
        initial = {
            "previous_scenes": [source.pk],
            "participants": list(
                source.scene_participants.values_list("player_id", flat=True)
            ),
            "dialogue_language": source.dialogue_language,
            "mode": source.mode,
        }

    form = SceneWorkbenchForm(
        request.POST if request.method == "POST" else None,
        instance=instance, campaign=campaign, initial=initial,
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
                previous_ids = set(
                    locked.previous_scenes.values_list("pk", flat=True)
                )
                selected_previous = {
                    predecessor.pk
                    for predecessor in form.cleaned_data["previous_scenes"]
                }
                if previous_ids != selected_previous and (
                    locked.turns.exists()
                    or locked.messages.exists()
                    or locked.gm_executions.exists()
                    or locked.next_scenes.exists()
                ):
                    return HttpResponse(
                        "Inherited history is locked for a played or referenced scene. Reload.",
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

            # Recheck predecessor validity against current database state
            # inside the write transaction, not just during form validation.
            previous_sources = list(
                form.cleaned_data["previous_scenes"]
            )
            valid_sources = list(
                campaign.scenes.filter(
                    pk__in=[source.pk for source in previous_sources],
                    is_closed=True,
                )
            )
            if len(valid_sources) != len(previous_sources) or any(
                scene is not None and any(
                    ancestor.pk == scene.pk
                    for ancestor in get_scene_lineage(source)
                )
                for source in valid_sources
            ):
                return HttpResponse(
                    "Previous scenes changed while editing. Reload.", status=409
                )

            saved.save()
            saved.previous_scenes.set(valid_sources)

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
