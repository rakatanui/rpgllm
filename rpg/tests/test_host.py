"""Smoke test: Django accepts Host: mraz.local and does not require ALLOWED_HOSTS=['*']."""
import os
import pytest
from django.test import Client, override_settings


@pytest.mark.django_db
def test_accepts_mraz_local_host():
    c = Client(HTTP_HOST="mraz.local")
    r = c.get("/health/")
    assert r.status_code == 200


@pytest.mark.django_db
def test_rejects_unknown_host_without_star():
    # Default settings should NOT allow arbitrary hosts.
    c = Client(HTTP_HOST="evil.example.com")
    r = c.get("/health/")
    assert r.status_code == 400


def test_allowed_hosts_does_not_contain_star():
    from django.conf import settings
    assert "*" not in settings.ALLOWED_HOSTS


def test_staticfiles_use_whitenoise_configuration():
    from django.conf import settings

    assert "whitenoise.middleware.WhiteNoiseMiddleware" in settings.MIDDLEWARE
    assert (
        settings.STORAGES["staticfiles"]["BACKEND"]
        == "whitenoise.storage.CompressedManifestStaticFilesStorage"
    )
