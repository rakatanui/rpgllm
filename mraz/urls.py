"""Root URL configuration for MRAZ Master."""
from django.contrib import admin
from django.urls import include, path
from django.http import JsonResponse


def healthz(request):
    return JsonResponse({"ok": True})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", healthz),
    path("", include("rpg.urls")),
]