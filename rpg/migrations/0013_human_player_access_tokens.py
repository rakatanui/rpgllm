# Generated manually for scene-scoped HUMAN player access.
import uuid

from django.db import migrations, models


def populate_human_access_tokens(apps, schema_editor):
    SceneParticipant = apps.get_model("rpg", "SceneParticipant")
    for participation in SceneParticipant.objects.all().iterator():
        participation.human_access_token = uuid.uuid4()
        participation.save(update_fields=["human_access_token"])


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0012_model_game_master"),
    ]

    operations = [
        migrations.AddField(
            model_name="sceneparticipant",
            name="human_access_enabled",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Disable to revoke this scene-specific HUMAN player link immediately."
                ),
            ),
        ),
        migrations.AddField(
            model_name="sceneparticipant",
            name="human_access_token",
            field=models.UUIDField(
                blank=True,
                editable=False,
                help_text="Secret bearer token for the HUMAN player client in this scene.",
                null=True,
            ),
        ),
        migrations.RunPython(
            populate_human_access_tokens,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="sceneparticipant",
            name="human_access_token",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                help_text="Secret bearer token for the HUMAN player client in this scene.",
                unique=True,
            ),
        ),
    ]
