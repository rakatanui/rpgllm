"""Django admin for MRAZ Master."""
from django.contrib import admin
from django import forms

from rpg.models import Campaign, LoreEntry, Message, ModelConfig, Player, Scene, Turn, TurnExecution


@admin.register(ModelConfig)
class ModelConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "gateway_model", "temperature", "enabled")
    list_editable = ("enabled",)
    list_filter = ("enabled",)
    search_fields = ("name", "gateway_model")


class PlayerInline(admin.TabularInline):
    model = Player
    extra = 1
    fields = ("display_name", "character_prompt", "model_config", "status")
    readonly_fields = ("status",)
    show_change_link = True


class SceneInline(admin.TabularInline):
    model = Scene
    extra = 1
    fields = ("name", "description", "mode", "round_order", "active_player_index")
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


@admin.register(Scene)
class SceneAdmin(admin.ModelAdmin):
    list_display = ("name", "campaign", "mode", "active_player_index", "created_at")
    list_filter = ("campaign", "mode")
    search_fields = ("name",)


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("display_name", "campaign", "model_config", "status", "created_at")
    list_filter = ("campaign", "status")
    search_fields = ("display_name",)
    fields = (
        "campaign",
        "display_name",
        "character_prompt",
        "memory_summary",
        "model_config",
        "status",
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
    list_display = ("pk", "scene", "author_type", "author_player", "visibility",
                    "private_player", "action_type", "created_at")
    list_filter = ("visibility", "author_type", "action_type")
    search_fields = ("content",)
    readonly_fields = ("created_at",)

@admin.register(TurnExecution)
class TurnExecutionAdmin(admin.ModelAdmin):
    list_display = ("pk", "turn", "player", "order_index", "state", "action_type", "updated_at")
    list_filter = ("state", "action_type")
    search_fields = ("player__display_name", "error")
    readonly_fields = ("created_at", "updated_at")
