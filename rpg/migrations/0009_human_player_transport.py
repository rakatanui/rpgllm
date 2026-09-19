from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0008_manual_chat_transport"),
    ]

    operations = [
        migrations.AlterField(
            model_name="player",
            name="transport",
            field=models.CharField(
                choices=[
                    ("LITELLM", "LiteLLM / API"),
                    ("MANUAL_CHAT", "Manual external chat"),
                    ("HUMAN", "Human player"),
                ],
                default="LITELLM",
                max_length=30,
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
                    ("waiting_human", "Waiting for human player"),
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
                    ("WAITING_HUMAN", "Waiting for human player"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                    ("INVALID", "Invalid response"),
                ],
                default="PENDING",
                max_length=20,
            ),
        ),
    ]
