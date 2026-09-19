from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0007_gm_workbench"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="transport",
            field=models.CharField(
                choices=[
                    ("LITELLM", "LiteLLM / API"),
                    ("MANUAL_CHAT", "Manual external chat"),
                ],
                default="LITELLM",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="manual_chat_label",
            field=models.CharField(
                blank=True,
                help_text="Human label shown in the GM UI, e.g. ChatGPT 5.6 or Claude.",
                max_length=120,
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="manual_chat_url",
            field=models.URLField(
                blank=True,
                help_text="Optional URL of the persistent external chat used for this player.",
                max_length=1000,
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="manual_chat_context_mode",
            field=models.CharField(
                choices=[
                    ("FULL", "Full prompt every turn"),
                    ("CHAT_MEMORY", "Use external chat memory after bootstrap"),
                ],
                default="FULL",
                help_text=(
                    "FULL sends complete application context every turn. CHAT_MEMORY sends "
                    "one full bootstrap, then only newly visible context and current turn constraints."
                ),
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="manual_chat_initialized",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "True after a successful bootstrap response was imported for CHAT_MEMORY. "
                    "Reset this if the external conversation is replaced or loses its memory."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="player",
            name="status",
            field=models.CharField(
                choices=[
                    ("idle", "Idle"),
                    ("generating", "Generating"),
                    ("waiting_external", "Waiting for external chat"),
                    ("error", "Error"),
                ],
                default="idle",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="turnexecution",
            name="state",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("RUNNING", "Running"),
                    ("WAITING_EXTERNAL", "Waiting for external chat"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                    ("INVALID", "Invalid response"),
                ],
                default="PENDING",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="transport",
            field=models.CharField(
                choices=[
                    ("LITELLM", "LiteLLM / API"),
                    ("MANUAL_CHAT", "Manual external chat"),
                ],
                default="LITELLM",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="external_prompt",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="external_context_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("FULL", "Full prompt every turn"),
                    ("CHAT_MEMORY", "Use external chat memory after bootstrap"),
                ],
                default="",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="external_chat_label",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="external_chat_url",
            field=models.URLField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="external_is_bootstrap",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="turnexecution",
            name="external_synced_message_ids",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
