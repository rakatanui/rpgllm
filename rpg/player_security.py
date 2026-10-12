"""Player bearer-token safeguards and database-backed request limits."""

import hashlib
import logging
import re
import time
from datetime import datetime, timezone as utc_timezone

from django.conf import settings
from django.db import models
from django.http import HttpResponse
from django.utils import timezone

_TOKEN_PATH = re.compile(
    r"(/play/)[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", re.IGNORECASE
)


class PlayerTokenRedactionFilter(logging.Filter):
    """Strip bearer UUIDs from Django/application log records, including errors."""

    def filter(self, record):
        message = record.getMessage()
        if "/play/" in message:
            record.msg = _TOKEN_PATH.sub(r"\1[redacted]", message)
            record.args = ()
        return True


def throttle_player_request(access_token, category):
    """Atomic fixed-window limit, keyed by a digest rather than a bearer token.

    This is called only *after* token validation: untrusted random tokens do
    not create rows in our database. DB-backed increments work across workers.
    """
    from rpg.models import PlayerRateBucket

    limit, window = settings.PLAYER_RATE_LIMITS[category]
    if limit <= 0:
        return None

    now = int(time.time())
    bucket_start = now - (now % window)
    secret = f"{access_token}:{category}:{bucket_start}".encode("ascii")
    key = hashlib.sha256(secret).hexdigest()

    PlayerRateBucket.objects.get_or_create(
        key=key,
        defaults={
            "expires_at": datetime.fromtimestamp(bucket_start + window, tz=utc_timezone.utc)
        },
    )
    updated = PlayerRateBucket.objects.filter(key=key, count__lt=limit).update(
        count=models.F("count") + 1
    )
    if updated:
        return None

    response = HttpResponse("Too many player requests. Please retry shortly.", status=429)
    response["Retry-After"] = str(max(1, bucket_start + window - now))
    return response
