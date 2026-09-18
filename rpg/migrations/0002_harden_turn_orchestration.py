# Generated manually for turn orchestration hardening.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rpg", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="turn",
            name="active_player_id_snapshot",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="turn",
            name="client_turn_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="turn",
            name="is_private",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="turn",
            name="round_advanced",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="TurnExecution",
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
                ("order_index", models.PositiveIntegerField(default=0)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("RUNNING", "Running"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                            ("INVALID", "Invalid response"),
                        ],
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                ("action_type", models.CharField(blank=True, default="", max_length=30)),
                ("error", models.TextField(blank=True)),
                ("history_message_ids", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "player",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="turn_executions",
                        to="rpg.player",
                    ),
                ),
                (
                    "turn",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="executions",
                        to="rpg.turn",
                    ),
                ),
            ],
            options={
                "ordering": ["order_index", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="turnexecution",
            constraint=models.UniqueConstraint(
                fields=("turn", "player"),
                name="uniq_turn_execution_player",
            ),
        ),
        migrations.AddConstraint(
            model_name="turn",
            constraint=models.UniqueConstraint(
                condition=models.Q(("client_turn_id__isnull", False)),
                fields=("scene", "client_turn_id"),
                name="uniq_scene_client_turn_id",
            ),
        ),
        migrations.RemoveField(
            model_name="message",
            name="private_to_gm",
        ),
        migrations.AddField(
            model_name="message",
            name="execution",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="messages",
                to="rpg.turnexecution",
            ),
        ),
    ]
