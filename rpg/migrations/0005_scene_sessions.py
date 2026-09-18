from django.db import migrations, models
import django.db.models.deletion


def populate_existing_scene_participants(apps, schema_editor):
    Scene = apps.get_model("rpg", "Scene")
    Player = apps.get_model("rpg", "Player")
    SceneParticipant = apps.get_model("rpg", "SceneParticipant")

    rows = []
    for scene in Scene.objects.all().iterator():
        players = Player.objects.filter(campaign_id=scene.campaign_id).order_by("created_at", "pk")
        for index, player in enumerate(players):
            rows.append(
                SceneParticipant(
                    scene_id=scene.pk,
                    player_id=player.pk,
                    order=index,
                )
            )
    if rows:
        SceneParticipant.objects.bulk_create(rows, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0004_context_manager"),
    ]

    operations = [
        migrations.AddField(
            model_name="scene",
            name="closed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="scene",
            name="is_closed",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Closed scenes are read-only and may only be used as history for later scenes."
                ),
            ),
        ),
        migrations.AddField(
            model_name="scene",
            name="previous_scenes",
            field=models.ManyToManyField(
                blank=True,
                help_text=(
                    "Closed earlier scenes whose visible history is inherited as backstory. "
                    "A scene may have multiple predecessors when parallel threads converge."
                ),
                related_name="next_scenes",
                symmetrical=False,
                to="rpg.scene",
            ),
        ),
        migrations.CreateModel(
            name="SceneParticipant",
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
                ("order", models.PositiveSmallIntegerField(default=0)),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="scene_participations",
                        to="rpg.player",
                    ),
                ),
                (
                    "scene",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="scene_participants",
                        to="rpg.scene",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="sceneparticipant",
            constraint=models.UniqueConstraint(
                fields=("scene", "player"),
                name="uniq_scene_participant",
            ),
        ),
        migrations.AddField(
            model_name="scene",
            name="participants",
            field=models.ManyToManyField(
                blank=True,
                help_text="Players who are actually present in / can act in this scene.",
                related_name="scenes",
                through="rpg.SceneParticipant",
                to="rpg.player",
            ),
        ),
        migrations.RunPython(
            populate_existing_scene_participants,
            migrations.RunPython.noop,
        ),
    ]
