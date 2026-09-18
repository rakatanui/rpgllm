"""URLs for the rpg app."""
from django.urls import path
from django.contrib.admin.views.decorators import staff_member_required
from . import views

urlpatterns = [
    path("", views.campaigns, name="campaigns"),
    path("scene/<int:scene_id>/", views.scene_view, name="scene"),
    path("scene/<int:scene_id>/send/", views.send_gm_message, name="send_gm_message"),
    path("scene/<int:scene_id>/send-private/<int:player_id>/", views.send_private_message,
         name="send_private_message"),
    path("scene/<int:scene_id>/private/<int:player_id>/read/", views.mark_private_read,
         name="mark_private_read"),
    path("scene/<int:scene_id>/mode/", views.set_mode, name="set_mode"),
    path("scene/<int:scene_id>/refresh/", views.scene_fragment, name="scene_fragment"),
    path("scene/<int:scene_id>/players-status/", views.players_status,
         name="players_status"),
    path("player/<int:player_id>/messages/<str:visibility>/", views.player_messages,
         name="player_messages"),
]