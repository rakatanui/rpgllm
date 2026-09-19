from django.db import migrations, models
import rpg.models


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0009_human_player_transport"),
    ]

    operations = [
        migrations.AddField(
            model_name="player",
            name="character_image",
            field=models.ImageField(
                blank=True,
                help_text="Portrait shown on the human-player character card.",
                upload_to=rpg.models.character_image_upload_to,
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="character_summary",
            field=models.TextField(
                blank=True,
                help_text="Player-facing short character description / identity summary.",
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="characteristics",
            field=models.TextField(
                blank=True,
                help_text="Player-facing characteristics, stats, traits, or other sheet data.",
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="abilities",
            field=models.TextField(
                blank=True,
                help_text="Player-facing abilities, powers, skills, spells, or special rules.",
            ),
        ),
    ]
