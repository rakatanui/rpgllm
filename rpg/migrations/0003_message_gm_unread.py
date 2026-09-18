# Generated manually for GM private unread markers.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0002_harden_turn_orchestration"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="gm_unread",
            field=models.BooleanField(
                default=False,
                help_text="True for a player private message the GM has not opened yet.",
            ),
        ),
    ]
