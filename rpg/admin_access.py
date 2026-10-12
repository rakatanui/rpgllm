"""Consistent campaign ownership checks for Django Admin and related choices."""
from django.contrib import admin
from django.core.exceptions import PermissionDenied

from rpg.models import (
    Campaign,
    CampaignPlan,
    CharacterAppearance,
    GameMasterConfig,
    GameMasterExecution,
    LoreEntry,
    Message,
    MessageRevision,
    Player,
    Scene,
    SceneParticipant,
    Turn,
    TurnExecution,
)


OWNERSHIP_PATHS = {
    Campaign: "owner_id",
    CampaignPlan: "campaign__owner_id",
    GameMasterConfig: "campaign__owner_id",
    Scene: "campaign__owner_id",
    Player: "campaign__owner_id",
    LoreEntry: "campaign__owner_id",
    SceneParticipant: "scene__campaign__owner_id",
    CharacterAppearance: "player__campaign__owner_id",
    Turn: "scene__campaign__owner_id",
    TurnExecution: "turn__scene__campaign__owner_id",
    GameMasterExecution: "scene__campaign__owner_id",
    Message: "campaign__owner_id",
    MessageRevision: "message__campaign__owner_id",
}


def scoped_queryset(user, queryset):
    """Restrict admin lists, object lookups and FK/M2M selectors to owned data."""
    if user.is_superuser:
        return queryset
    owner_path = OWNERSHIP_PATHS.get(queryset.model)
    if owner_path is None:
        # Globally configured provider aliases are managed by superusers.
        return queryset.none()
    return queryset.filter(**{owner_path: user.pk})


def is_owned_by(user, obj):
    if user.is_superuser:
        return True
    if obj is None:
        return True
    owner_path = OWNERSHIP_PATHS.get(type(obj))
    if owner_path is None:
        return False
    return type(obj)._default_manager.filter(
        pk=obj.pk, **{owner_path: user.pk}
    ).exists()


class OwnedRelatedFieldsMixin:
    """Prevent forged POSTs from referencing another master's records."""

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.remote_field.model in OWNERSHIP_PATHS and not request.user.is_superuser:
            queryset = db_field.remote_field.model._default_manager.all()
            kwargs["queryset"] = scoped_queryset(request.user, queryset)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        if db_field.remote_field.model in OWNERSHIP_PATHS and not request.user.is_superuser:
            queryset = db_field.remote_field.model._default_manager.all()
            kwargs["queryset"] = scoped_queryset(request.user, queryset)
        return super().formfield_for_manytomany(db_field, request, **kwargs)


class OwnedCampaignAdminMixin(OwnedRelatedFieldsMixin, admin.ModelAdmin):
    """Staff can manage their own data; superusers can manage all campaigns."""

    def get_queryset(self, request):
        return scoped_queryset(request.user, super().get_queryset(request))

    def has_module_permission(self, request):
        return bool(request.user.is_active and request.user.is_staff)

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request) and is_owned_by(request.user, obj)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self.has_view_permission(request, obj)

    def get_list_filter(self, request):
        filters = super().get_list_filter(request)
        if request.user.is_superuser:
            return filters
        # RelatedFieldListFilter otherwise queries unscoped related records
        # and can reveal foreign campaign/player names.
        return tuple(
            item for item in filters
            if not (
                isinstance(item, str)
                and any(part in item for part in ("campaign", "scene", "player", "turn"))
            )
        )

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if self.model is Campaign and not request.user.is_superuser:
            return (*fields, "owner")
        return fields

    def save_model(self, request, obj, form, change):
        if self.model is Campaign:
            if not request.user.is_superuser:
                obj.owner = request.user
            elif obj.owner_id is None:
                obj.owner = request.user
        elif not is_owned_by(request.user, obj) and change:
            raise PermissionDenied
        super().save_model(request, obj, form, change)


class OwnedCampaignInlineMixin(OwnedRelatedFieldsMixin):
    """Inlines are usable by masters only when the parent belongs to them."""

    def _allowed(self, request, obj):
        return bool(
            request.user.is_active
            and request.user.is_staff
            and is_owned_by(request.user, obj)
        )

    def get_queryset(self, request):
        return scoped_queryset(request.user, super().get_queryset(request))

    def has_view_permission(self, request, obj=None):
        return self._allowed(request, obj)

    def has_add_permission(self, request, obj=None):
        return self._allowed(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request, obj)
