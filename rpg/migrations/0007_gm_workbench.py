from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0006_scene_dialogue_language"),
    ]

    operations = [
        migrations.AddField(
            model_name="scene",
            name="close_summary_draft",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="GM-reviewed draft produced before closing a scene.",
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="fallback_model_config",
            field=models.ForeignKey(
                blank=True,
                help_text="Optional model used for one-off fallback retries.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="fallback_players",
                to="rpg.modelconfig",
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="pending_nudge",
            field=models.TextField(
                blank=True,
                help_text="One-shot private GM instruction consumed by the player's next execution.",
            ),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="nudge_text",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="model_used",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="system_prompt_snapshot",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="request_messages",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="raw_response",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="latency_ms",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="MessageRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("revision_index", models.PositiveIntegerField()),
                ("content", models.TextField()),
                ("action_type", models.CharField(blank=True, default="", max_length=30)),
                ("reason", models.CharField(default="ORIGINAL", max_length=30)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("message", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="revisions", to="rpg.message")),
            ],
            options={"ordering": ["revision_index", "pk"]},
        ),
        migrations.AddConstraint(
            model_name="messagerevision",
            constraint=models.UniqueConstraint(fields=("message", "revision_index"), name="uniq_message_revision_index"),
        ),
    ]
