# Generated manually for SOFT_ROUND choice state.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0015_gamemasterexecution_scene_transition"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scene",
            name="mode",
            field=models.CharField(
                choices=[
                    ("MANUAL", "Manual"),
                    ("ROUND", "Round"),
                    ("SOFT_ROUND", "Soft round / parallel lines"),
                    ("SIMULTANEOUS", "Simultaneous"),
                    ("TABLE", "Table"),
                ],
                default="ROUND",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="turn",
            name="mode",
            field=models.CharField(
                choices=[
                    ("MANUAL", "Manual"),
                    ("ROUND", "Round"),
                    ("SOFT_ROUND", "Soft round / parallel lines"),
                    ("SIMULTANEOUS", "Simultaneous"),
                    ("TABLE", "Table"),
                ],
                max_length=20,
            ),
        ),
    ]
