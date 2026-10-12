"""Root URL configuration for MRAZ Master."""
from django.contrib import admin
from django.urls import include, path
from django.http import JsonResponse
from django.contrib.auth.views import LogoutView

from rpg.auth_views import GameMasterLoginView


def healthz(request):
    return JsonResponse({"ok": True})


urlpatterns = [
    path("auth/login/", GameMasterLoginView.as_view(), name="gm_login"),
    path("auth/logout/", LogoutView.as_view(next_page="gm_login"), name="gm_logout"),
    path("admin/", admin.site.urls),
    path("health/", healthz),
    path("", include("rpg.urls")),
]