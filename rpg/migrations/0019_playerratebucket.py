"""Persist fixed-window player request counters shared between web workers."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("rpg", "0018_alter_turnexecution_transport")]

    operations = [
        migrations.CreateModel(
            name="PlayerRateBucket",
            fields=[
                ("key", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("count", models.PositiveIntegerField(default=0)),
                ("expires_at", models.DateTimeField(db_index=True)),
            ],
            options={"ordering": ["expires_at"]},
        ),
    ]
