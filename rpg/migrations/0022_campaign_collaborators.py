"""Allow multiple authenticated staff masters to share one campaign."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0021_campaign_lifecycle"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CampaignCollaborator",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("added_at", models.DateTimeField(auto_now_add=True)),
                ("campaign", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="collaborators", to="rpg.campaign",
                )),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="rpg_collaborations", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "ordering": ["added_at", "pk"],
                "constraints": [models.UniqueConstraint(
                    fields=["campaign", "user"], name="uniq_campaign_collaborator",
                )],
            },
        ),
    ]
