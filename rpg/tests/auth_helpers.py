"""Test client for the authenticated GM workbench (not anonymous-player tests)."""

from django.contrib.auth import get_user_model
from django.test import Client


def gm_client(**kwargs):
    client = Client(**kwargs)
    user_model = get_user_model()
    user, _ = user_model.objects.get_or_create(
        username="__rpg_test_gm__",
        defaults={"is_staff": True, "is_active": True},
    )
    client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
    return client
