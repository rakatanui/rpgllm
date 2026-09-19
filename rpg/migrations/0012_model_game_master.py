# Generated manually for model Game Master support.
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0011_character_appearances"),
    ]

    operations = [
        migrations.CreateModel(
            name="GameMasterConfig",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("enabled", models.BooleanField(default=False)),
                (
                    "transport",
                    models.CharField(
                        choices=[
                            ("LITELLM", "LiteLLM / API"),
                            ("MANUAL_CHAT", "Manual external chat"),
                        ],
                        default="LITELLM",
                        max_length=30,
                    ),
                ),
                (
                    "system_prompt",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Authoritative instructions for the model acting as Game Master. "
                            "Campaign, scene, lore, characters and all GM-visible history "
                            "are added separately."
                        ),
                    ),
                ),
                (
                    "review_before_publish",
                    models.BooleanField(
                        default=True,
                        help_text=(
                            "When enabled, model output becomes an editable draft. "
                            "When disabled, a valid result is published immediately."
                        ),
                    ),
                ),
                ("manual_chat_label", models.CharField(blank=True, max_length=120)),
                ("manual_chat_url", models.URLField(blank=True, max_length=1000)),
                (
                    "manual_chat_context_mode",
                    models.CharField(
                        choices=[
                            ("FULL", "Full prompt every turn"),
                            ("CHAT_MEMORY", "Use external chat memory after bootstrap"),
                        ],
                        default="FULL",
                        max_length=30,
                    ),
                ),
                ("manual_chat_initialized", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "campaign",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="gm_config",
                        to="rpg.campaign",
                    ),
                ),
                (
                    "fallback_model_config",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="gm_fallback_configs",
                        to="rpg.modelconfig",
                    ),
                ),
                (
                    "model_config",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="gm_configs",
                        to="rpg.modelconfig",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="GameMasterExecution",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RUNNING", "Running"),
                            ("WAITING_EXTERNAL", "Waiting for external chat"),
                            ("DRAFT", "Draft ready"),
                            ("PUBLISHED", "Published"),
                            ("FAILED", "Failed"),
                            ("INVALID", "Invalid response"),
                            ("DISCARDED", "Discarded"),
                        ],
                        default="PENDING",
                        max_length=30,
                    ),
                ),
                (
                    "transport",
                    models.CharField(
                        choices=[
                            ("LITELLM", "LiteLLM / API"),
                            ("MANUAL_CHAT", "Manual external chat"),
                        ],
                        default="LITELLM",
                        max_length=30,
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("TURN", "Publish and run player turn"),
                            ("NARRATE", "Publish without player turn"),
                            ("WAIT", "Do nothing"),
                        ],
                        default="",
                        max_length=20,
                    ),
                ),
                ("public_draft", models.TextField(blank=True)),
                ("private_drafts", models.JSONField(blank=True, default=list)),
                ("turn_targets", models.JSONField(blank=True, default=list)),
                ("error", models.TextField(blank=True)),
                ("context_message_ids", models.JSONField(blank=True, default=list)),
                ("model_used", models.CharField(blank=True, default="", max_length=200)),
                ("system_prompt_snapshot", models.TextField(blank=True)),
                ("request_messages", models.JSONField(blank=True, default=list)),
                ("raw_response", models.TextField(blank=True)),
                ("latency_ms", models.PositiveIntegerField(blank=True, null=True)),
                ("external_prompt", models.TextField(blank=True)),
                (
                    "external_context_mode",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("FULL", "Full prompt every turn"),
                            ("CHAT_MEMORY", "Use external chat memory after bootstrap"),
                        ],
                        default="",
                        max_length=30,
                    ),
                ),
                ("external_chat_label", models.CharField(blank=True, default="", max_length=120)),
                ("external_chat_url", models.URLField(blank=True, default="", max_length=1000)),
                ("external_is_bootstrap", models.BooleanField(default=False)),
                ("external_synced_message_ids", models.JSONField(blank=True, default=list)),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "config",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="executions",
                        to="rpg.gamemasterconfig",
                    ),
                ),
                (
                    "published_turn",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="gm_executions",
                        to="rpg.turn",
                    ),
                ),
                (
                    "scene",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="gm_executions",
                        to="rpg.scene",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-pk"],
            },
        ),
    ]
