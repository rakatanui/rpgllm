"""Host-level isolation for the Internet-facing HUMAN player endpoint."""
from django.conf import settings
from django.http import HttpResponseNotFound


class PublicPlayerHostMiddleware:
    """Allow only player-client and static paths on PUBLIC_PLAYER_HOST.

    This is defense in depth behind the player-edge reverse proxy. If the
    Cloudflare Tunnel is accidentally pointed directly at Django, the public
    hostname still cannot reach the GM workbench or Django Admin.
    """

    ALLOWED_PREFIXES = ("/play/", "/static/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        public_host = getattr(settings, "PUBLIC_PLAYER_HOST", "")
        if public_host:
            request_host = request.get_host().split(":", 1)[0].lower()
            if (
                request_host == public_host
                and not request.path.startswith(self.ALLOWED_PREFIXES)
            ):
                return HttpResponseNotFound("Not found.")
        return self.get_response(request)
