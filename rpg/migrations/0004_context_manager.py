# Generated manually for context-manager MVP.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0003_message_gm_unread"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="shared_memory",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Compact shared campaign memory: established public facts and important "
                    "events that should survive history trimming."
                ),
            ),
        ),
        migrations.AddField(
            model_name="player",
            name="memory_summary",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Compact long-term memory known only to this player: secrets, promises, "
                    "relationships, intentions and older important events."
                ),
            ),
        ),
        migrations.AddField(
            model_name="scene",
            name="memory_summary",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Compact summary of older events/state for this scene. It is always "
                    "included in player context even when old messages are trimmed."
                ),
            ),
        ),
        migrations.CreateModel(
            name="LoreEntry",
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
                ("title", models.CharField(max_length=200)),
                ("category", models.CharField(blank=True, max_length=80)),
                ("content", models.TextField()),
                (
                    "scope",
                    models.CharField(
                        choices=[
                            ("GLOBAL", "Global"),
                            ("SCENE", "Scene-specific"),
                            ("PLAYER", "Player-specific"),
                        ],
                        default="GLOBAL",
                        help_text=(
                            "GLOBAL: all campaign players. SCENE: only assigned scenes. "
                            "PLAYER: only assigned players."
                        ),
                        max_length=20,
                    ),
                ),
                (
                    "priority",
                    models.PositiveSmallIntegerField(
                        default=100,
                        help_text=(
                            "Lower values are included first when the lore context budget is full."
                        ),
                    ),
                ),
                ("enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "campaign",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lore_entries",
                        to="rpg.campaign",
                    ),
                ),
                (
                    "players",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Used when scope is PLAYER.",
                        related_name="lore_entries",
                        to="rpg.player",
                    ),
                ),
                (
                    "scenes",
                    models.ManyToManyField(
                        blank=True,
                        help_text="Used when scope is SCENE.",
                        related_name="lore_entries",
                        to="rpg.scene",
                    ),
                ),
            ],
            options={
                "ordering": ["priority", "title", "pk"],
            },
        ),
    ]
