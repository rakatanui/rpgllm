"""URLs for the rpg app."""
from django.urls import path
from django.contrib.admin.views.decorators import staff_member_required
from . import views

urlpatterns = [
    path("", views.campaigns, name="campaigns"),
    path("scene/<int:scene_id>/", views.scene_view, name="scene"),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/",
        views.human_player_client,
        name="human_player_client",
    ),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/fragment/",
        views.human_player_fragment,
        name="human_player_fragment",
    ),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/character-image/",
        views.human_character_image,
        name="human_character_image",
    ),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/character-image/upload/",
        views.upload_human_character_image,
        name="upload_human_character_image",
    ),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/character-image/remove/",
        views.remove_human_character_image,
        name="remove_human_character_image",
    ),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/episodes/",
        views.human_episode_search,
        name="human_episode_search",
    ),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/episodes/<int:episode_id>/",
        views.human_episode_detail,
        name="human_episode_detail",
    ),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/execution/<int:execution_id>/submit/",
        views.submit_human_response,
        name="submit_human_response",
    ),
    path(
        "scene/<int:scene_id>/human/<int:player_id>/ooc/",
        views.human_send_ooc,
        name="human_send_ooc",
    ),
    path("scene/<int:scene_id>/history-search/", views.search_history, name="search_history"),
    path("scene/<int:scene_id>/send/", views.send_gm_message, name="send_gm_message"),
    path("scene/<int:scene_id>/silence/", views.silent_turn, name="silent_turn"),
    path("scene/<int:scene_id>/send-private/<int:player_id>/", views.send_private_message,
         name="send_private_message"),
    path("scene/<int:scene_id>/private/<int:player_id>/read/", views.mark_private_read,
         name="mark_private_read"),
    path("scene/<int:scene_id>/private/<int:player_id>/", views.private_channel,
         name="private_channel"),
    path(
        "scene/<int:scene_id>/external/<int:execution_id>/submit/",
        views.submit_external_response,
        name="submit_external_response",
    ),
    path(
        "scene/<int:scene_id>/manual-chat/<int:player_id>/reset-memory/",
        views.reset_manual_chat_memory,
        name="reset_manual_chat_memory",
    ),
    path("scene/<int:scene_id>/mode/", views.set_mode, name="set_mode"),
    path(
        "scene/<int:scene_id>/retry/<int:execution_id>/",
        views.retry_execution,
        name="retry_execution",
    ),
    path(
        "scene/<int:scene_id>/retry-fallback/<int:execution_id>/",
        views.retry_execution_fallback,
        name="retry_execution_fallback",
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
    path(
        "scene/<int:scene_id>/nudge/<int:player_id>/",
        views.set_player_nudge,
        name="set_player_nudge",
    ),
    path(
        "scene/<int:scene_id>/ooc-meta/",
        views.send_ooc_meta,
        name="send_ooc_meta",
    ),
    path(
        "scene/<int:scene_id>/execution/<int:execution_id>/debug/",
        views.execution_debug,
        name="execution_debug",
    ),
    path(
        "scene/<int:scene_id>/message/<int:message_id>/versions/",
        views.message_versions,
        name="message_versions",
    ),
    path(
        "scene/<int:scene_id>/message/<int:message_id>/restore/<int:revision_id>/",
        views.restore_message_version,
        name="restore_message_version",
    ),
    path(
        "scene/<int:scene_id>/undo-latest/",
        views.undo_latest_turn,
        name="undo_latest_turn",
    ),
    path(
        "scene/<int:scene_id>/message/<int:message_id>/pin-memory/",
        views.pin_message_memory,
        name="pin_message_memory",
    ),
    path(
        "scene/<int:scene_id>/prepare-close/",
        views.prepare_close_summary,
        name="prepare_close_summary",
    ),
    path(
        "scene/<int:scene_id>/apply-close/",
        views.apply_close_summary,
        name="apply_close_summary",
    ),
    path(
        "scene/<int:scene_id>/discard-close-summary/",
        views.discard_close_summary,
        name="discard_close_summary",
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