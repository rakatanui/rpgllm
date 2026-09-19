from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0013_human_player_access_tokens"),
    ]

    operations = [
        migrations.AddField(
            model_name="gamemasterconfig",
            name="auto_continue",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "After a HUMAN player finishes the current turn, automatically ask the model GM "
                    "for the next beat. For uninterrupted phone play, disable review_before_publish."
                ),
            ),
        ),
    ]
