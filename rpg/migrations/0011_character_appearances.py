# Generated manually for character appearance support.
import django.db.models.deletion
from django.db import migrations, models
import rpg.models


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0010_player_character_card"),
    ]

    operations = [
        migrations.CreateModel(
            name="CharacterAppearance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Player-facing description of this form. It is also included in the "
                            "model context when this is the current appearance."
                        ),
                    ),
                ),
                (
                    "portrait_image",
                    models.ImageField(
                        blank=True,
                        help_text="Square/cropped portrait for compact character cards.",
                        upload_to=rpg.models.character_appearance_upload_to,
                    ),
                ),
                (
                    "fullbody_image",
                    models.ImageField(
                        blank=True,
                        help_text="Full-body/reference image shown without cropping.",
                        upload_to=rpg.models.character_appearance_upload_to,
                    ),
                ),
                (
                    "is_primary",
                    models.BooleanField(
                        default=False,
                        help_text="Fallback appearance when a scene has no explicit current form.",
                    ),
                ),
                ("order", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="appearances",
                        to="rpg.player",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="characterappearance",
            constraint=models.UniqueConstraint(
                fields=("player", "name"),
                name="uniq_character_appearance_name_per_player",
            ),
        ),
        migrations.AddConstraint(
            model_name="characterappearance",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_primary=True),
                fields=("player",),
                name="uniq_primary_character_appearance_per_player",
            ),
        ),
        migrations.AddField(
            model_name="sceneparticipant",
            name="current_appearance",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Appearance currently used by this character in this scene. "
                    "Leave empty to use the primary appearance."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="scene_participations",
                to="rpg.characterappearance",
            ),
        ),
    ]
