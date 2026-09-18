import os
import django

# Configure Django before importing models.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mraz.settings")
if not django.apps.apps.ready:
    django.setup()

import pytest
from django.test import override_settings
from rpg.services.llm import reset_llm_client, MockLLMClient


@pytest.fixture(autouse=True)
def reset_client():
    reset_llm_client()
    yield
    reset_llm_client()


@pytest.fixture
def mock_backend():
    with override_settings(LLM_BACKEND="mock"):
        reset_llm_client()
        yield