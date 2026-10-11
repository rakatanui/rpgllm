"""Associate each campaign with a Game Master; legacy campaigns remain unassigned."""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("rpg", "0016_soft_round_mode"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="owner",
            field=models.ForeignKey(
                to=settings.AUTH_USER_MODEL,
                on_delete=django.db.models.deletion.PROTECT,
                null=True,
                blank=True,
                related_name="rpg_campaigns",
                help_text="Game Master responsible for this campaign; unassigned campaigns are superuser-only.",
            ),
        ),
    ]
