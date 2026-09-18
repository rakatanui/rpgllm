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
    path(
        "scene/<int:scene_id>/retry/<int:execution_id>/",
        views.retry_execution,
        name="retry_execution",
    ),
    path(
        "scene/<int:scene_id>/regenerate/<int:execution_id>/",
        views.regenerate_execution,
        name="regenerate_execution",
    ),
    path(
        "scene/<int:scene_id>/ooc/<int:message_id>/",
        views.ooc_revision,
        name="ooc_revision",
    ),
    path("scene/<int:scene_id>/close/", views.close_scene, name="close_scene"),
    path(
        "scene/<int:scene_id>/follow-up/",
        views.create_followup_scene,
        name="create_followup_scene",
    ),
    path("scene/<int:scene_id>/refresh/", views.scene_fragment, name="scene_fragment"),
    path("scene/<int:scene_id>/players-status/", views.players_status,
         name="players_status"),
    path("player/<int:player_id>/messages/<str:visibility>/", views.player_messages,
         name="player_messages"),
]