"""Synchronize TurnExecution transport choices with HUMAN transport."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0017_campaign_owner"),
    ]

    operations = [
        migrations.AlterField(
            model_name="turnexecution",
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
    ]
