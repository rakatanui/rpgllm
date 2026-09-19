"""Django admin for MRAZ Master."""
from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError

from rpg.models import (
    Campaign,
    CharacterAppearance,
    LoreEntry,
    Message,
    MessageRevision,
    ModelConfig,
    Player,
    Scene,
    SceneParticipant,
    Turn,
    TurnExecution,
)


@admin.register(ModelConfig)
class ModelConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "gateway_model", "temperature", "enabled")
    list_editable = ("enabled",)
    list_filter = ("enabled",)
    search_fields = ("name", "gateway_model")


class PlayerInline(admin.TabularInline):
    model = Player
    extra = 1
    fields = (
        "display_name",
        "character_prompt",
        "transport",
        "model_config",
        "fallback_model_config",
        "status",
    )
    readonly_fields = ("status",)
    show_change_link = True


class SceneInline(admin.TabularInline):
    model = Scene
    extra = 1
    fields = (
        "name",
        "description",
        "dialogue_language",
        "mode",
        "round_order",
        "active_player_index",
        "is_closed",
    )
    show_change_link = True


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)
    inlines = [PlayerInline, SceneInline]


class SceneForm(forms.ModelForm):
    class Meta:
        model = Scene
        fields = "__all__"

    def clean_previous_scenes(self):
        predecessors = self.cleaned_data.get("previous_scenes")
        campaign = self.cleaned_data.get("campaign")
        campaign_id = campaign.pk if campaign else self.instance.campaign_id
        if predecessors is None:
            return predecessors

        for predecessor in predecessors:
            if campaign_id and predecessor.campaign_id != campaign_id:
                raise ValidationError("A predecessor scene must belong to the same campaign.")
            if not predecessor.is_closed:
                raise ValidationError("Only closed scenes can be used as predecessor history.")
            if self.instance.pk and predecessor.pk == self.instance.pk:
                raise ValidationError("A scene cannot use itself as predecessor history.")
            if self.instance.pk and _scene_reaches(predecessor, self.instance.pk):
                raise ValidationError("Scene predecessor links cannot contain cycles.")
        return predecessors


def _scene_reaches(scene: Scene, target_scene_id: int) -> bool:
    pending = [scene]
    visited = set()
    while pending:
        current = pending.pop()
        if current.pk in visited:
            continue
        visited.add(current.pk)
        if current.pk == target_scene_id:
            return True
        pending.extend(current.previous_scenes.all())
    return False


class SceneParticipantInline(admin.TabularInline):
    model = SceneParticipant
    extra = 1
    fields = ("player", "order", "current_appearance")
    ordering = ("order", "pk")


@admin.register(Scene)
class SceneAdmin(admin.ModelAdmin):
    form = SceneForm
    list_display = (
        "name",
        "campaign",
        "mode",
        "is_closed",
        "active_player_index",
        "created_at",
    )
    list_filter = ("campaign", "mode", "is_closed")
    search_fields = ("name",)
    filter_horizontal = ("previous_scenes",)
    readonly_fields = ("closed_at",)
    inlines = [SceneParticipantInline]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "campaign",
                    "name",
                    "description",
                    "dialogue_language",
                    "memory_summary",
                )
            },
        ),
        (
            "Session flow",
            {
                "fields": ("previous_scenes", "is_closed", "closed_at"),
                "description": (
                    "Previous scenes act as inherited backstory. Only participants "
                    "of an earlier scene can see that scene's public history."
                ),
            },
        ),
        (
            "Turn engine",
            {
                "fields": ("mode", "round_order", "active_player_index"),
            },
        ),
    )


@admin.register(CharacterAppearance)
class CharacterAppearanceAdmin(admin.ModelAdmin):
    list_display = ("name", "player", "is_primary", "order", "updated_at")
    list_filter = ("player__campaign", "is_primary")
    search_fields = ("name", "player__display_name", "description")
    ordering = ("player", "order", "pk")


class CharacterAppearanceInline(admin.StackedInline):
    model = CharacterAppearance
    extra = 1
    fields = (
        "name",
        "description",
        "portrait_image",
        "fullbody_image",
        "is_primary",
        "order",
    )
    ordering = ("order", "pk")


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "campaign",
        "transport",
        "model_config",
        "manual_chat_context_mode",
        "status",
        "created_at",
    )
    list_filter = ("campaign", "transport", "manual_chat_context_mode", "status")
    search_fields = ("display_name",)
    inlines = [CharacterAppearanceInline]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "campaign",
                    "display_name",
                    "transport",
                    "status",
                )
            },
        ),
        (
            "Player-facing character card",
            {
                "fields": (
                    "character_image",
                    "character_summary",
                    "characteristics",
                    "abilities",
                    "memory_summary",
                ),
                "description": (
                    "Shown to a HUMAN player in the client. Memory summary is also "
                    "part of the player's private long-term context."
                ),
            },
        ),
        (
            "Model character context",
            {
                "fields": ("character_prompt", "pending_nudge"),
                "description": (
                    "character_prompt is authoritative model-facing character context. "
                    "It is not exposed verbatim in the HUMAN client."
                ),
            },
        ),
        (
            "Model / transport",
            {
                "fields": (
                    "model_config",
                    "fallback_model_config",
                    "manual_chat_label",
                    "manual_chat_url",
                    "manual_chat_context_mode",
                    "manual_chat_initialized",
                )
            },
        ),
    )


@admin.register(LoreEntry)
class LoreEntryAdmin(admin.ModelAdmin):
    list_display = ("title", "campaign", "category", "scope", "priority", "enabled", "updated_at")
    list_filter = ("campaign", "scope", "enabled", "category")
    search_fields = ("title", "category", "content")
    list_editable = ("priority", "enabled")
    filter_horizontal = ("scenes", "players")
    fieldsets = (
        (None, {"fields": ("campaign", "title", "category", "content")}),
        (
            "Context routing",
            {
                "fields": ("scope", "scenes", "players", "priority", "enabled"),
                "description": (
                    "GLOBAL ignores scene/player assignments. SCENE uses assigned scenes. "
                    "PLAYER uses assigned players."
                ),
            },
        ),
    )


@admin.register(Turn)
class TurnAdmin(admin.ModelAdmin):
    list_display = ("pk", "scene", "mode", "state", "created_at", "error")
    list_filter = ("state", "mode")
    readonly_fields = ("error",)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = (
        "pk",
        "scene",
        "author_type",
        "author_player",
        "visibility",
        "private_player",
        "action_type",
        "created_at",
    )
    list_filter = ("visibility", "author_type", "action_type")
    search_fields = ("content",)
    readonly_fields = ("created_at",)


@admin.register(TurnExecution)
class TurnExecutionAdmin(admin.ModelAdmin):
    list_display = ("pk", "turn", "player", "order_index", "state", "action_type", "updated_at")
    list_filter = ("state", "action_type")
    search_fields = ("player__display_name", "error")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MessageRevision)
class MessageRevisionAdmin(admin.ModelAdmin):
    list_display = ("message", "revision_index", "reason", "created_at")
    list_filter = ("reason",)
    search_fields = ("message__content", "content")
