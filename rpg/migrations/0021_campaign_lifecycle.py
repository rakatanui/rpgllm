"""Archive campaigns without deleting data; retain recoverable recycle-bin entries."""
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("rpg", "0020_campaign_timeline")]

    operations = [
        migrations.AddField(
            model_name="campaign", name="archived_at",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="Archived campaigns remain readable to their GM but cannot be played or edited.",
            ),
        ),
        migrations.AddField(
            model_name="campaign", name="deleted_at",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="Soft-deleted campaigns are hidden outside the owner's recycle bin.",
            ),
        ),
    ]
