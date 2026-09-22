from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0014_gamemasterconfig_auto_continue"),
    ]

    operations = [
        migrations.AddField(
            model_name="gamemasterexecution",
            name="scene_transition",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "Optional model-requested update of the live Scene label, for example "
                    '{"name": "Гданьск - Машина у кафе"}. Empty means no transition.'
                ),
            ),
        ),
    ]
