"""Remove expired fixed-window bearer request counters."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from rpg.models import PlayerRateBucket


class Command(BaseCommand):
    help = "Delete expired public player request counters (run daily in production)."

    def handle(self, *args, **options):
        expired = PlayerRateBucket.objects.filter(expires_at__lt=timezone.now())
        count = expired.count()
        expired.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {count} expired player rate buckets."))
