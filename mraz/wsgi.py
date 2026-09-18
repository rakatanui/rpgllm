"""WSGI config for MRAZ Master (kept for management commands)."""
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mraz.settings")

application = get_wsgi_application()