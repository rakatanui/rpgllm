from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0005_scene_sessions"),
    ]

    operations = [
        migrations.AddField(
            model_name="scene",
            name="dialogue_language",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Default diegetic language for spoken dialogue in this scene, "
                    "for example French, Portuguese, Russian. Direct speech is emitted "
                    "in this language with a Russian hover translation."
                ),
                max_length=80,
            ),
        ),
    ]
