"""Single authorization rule shared by workbench, middleware, and Django Admin.

An owner keeps lifecycle/team authority; an active staff collaborator gets
ordinary GM read/write access to a campaign. A superuser gets global access.
"""
from django.db.models import Q

from rpg.models import Campaign


def visible_campaigns(user):
    """Campaigns this authenticated staff user can GM, including archives.

    Deleted campaigns are intentionally excluded; owners use the recycle bin.
    """
    qs = Campaign.objects.filter(deleted_at__isnull=True)
    if not user.is_authenticated or not user.is_active or not user.is_staff:
        return qs.none()
    if user.is_superuser:
        return qs
    return qs.filter(
        Q(owner_id=user.pk) | Q(collaborators__user_id=user.pk),
    ).distinct()


def can_manage_team(user, campaign):
    """Lifecycle/team operations are reserved for the owner and superuser."""
    return bool(
        user.is_authenticated and user.is_active and user.is_staff
        and (user.is_superuser or campaign.owner_id == user.pk)
    )
