"""Private campaign-local clock and the Game Master's NPC forecast board."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0019_playerratebucket"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="world_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="campaign",
            name="world_time",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="CampaignPlan",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("NPC", "NPC plan"), ("MARKER", "Story marker")], default="NPC", max_length=12)),
                ("title", models.CharField(max_length=200)),
                ("npc_name", models.CharField(blank=True, max_length=160)),
                ("description", models.TextField(blank=True)),
                ("planned_outcome", models.TextField(blank=True, help_text="What would happen without player interference.")),
                ("actual_outcome", models.TextField(blank=True, help_text="What changed or what actually happened.")),
                ("scheduled_date", models.DateField()),
                ("scheduled_time", models.TimeField(blank=True, null=True)),
                ("end_date", models.DateField(blank=True, null=True)),
                ("end_time", models.TimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[
                    ("PLANNED", "Planned (if unopposed)"),
                    ("CHANGED", "Changed by player actions"),
                    ("CANCELLED", "Cancelled"),
                    ("COMPLETED", "Happened"),
                ], default="PLANNED", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("campaign", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="timeline_plans", to="rpg.campaign",
                )),
            ],
            options={
                "ordering": ["scheduled_date", "scheduled_time", "pk"],
                "indexes": [models.Index(fields=["campaign", "scheduled_date"], name="rpg_campplan_date_idx")],
            },
        ),
    ]
