"""Host-level isolation for the Internet-facing HUMAN player endpoint."""
from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.contrib.auth.views import redirect_to_login
from django.urls import Resolver404, resolve, reverse
from urllib.parse import urlencode


class PublicPlayerHostMiddleware:
    """Allow only player-client and static paths on PUBLIC_PLAYER_HOST.

    This is defense in depth behind the player-edge reverse proxy. If the
    Cloudflare Tunnel is accidentally pointed directly at Django, the public
    hostname still cannot reach the GM workbench or Django Admin.
    """

    ALLOWED_PREFIXES = ("/play/", "/static/rpg/")

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


class GameMasterAccessMiddleware:
    """Deny anonymous/non-staff access to the entire GM workbench by default.

    Only token-gated HUMAN endpoints and non-GM infrastructure paths are public.
    Django Admin retains its own staff authentication. Unlike a per-view
    decorator, this also protects future scene, debug and HTMX routes.
    """

    PUBLIC_PATHS = frozenset(("/auth/login/", "/auth/logout/", "/health/"))
    PUBLIC_PREFIXES = ("/play/", "/admin/", "/static/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info
        if path in self.PUBLIC_PATHS or path.startswith(self.PUBLIC_PREFIXES):
            return self.get_response(request)

        user = request.user
        if not user.is_authenticated:
            login_url = reverse("gm_login")
            next_url = request.get_full_path()
            if request.headers.get("HX-Request", "").lower() == "true":
                # HTMX must redirect the top-level browser, not inject the
                # login page into a polling fragment.
                response = HttpResponseForbidden("Game Master login required.")
                response["HX-Redirect"] = f"{login_url}?{urlencode({'next': next_url})}"
                return response
            return redirect_to_login(next_url, login_url)

        if not user.is_active or not user.is_staff:
            return HttpResponseForbidden("Game Master access required.")

        # All scene/player GM endpoints must be resolved through the owner of
        # the campaign; never trust numeric object IDs supplied in the URL.
        if not user.is_superuser:
            from rpg.models import Campaign, Player, Scene

            try:
                kwargs = resolve(path).kwargs
            except Resolver404:
                kwargs = {}
            campaign_id = kwargs.get("campaign_id")
            scene_id = kwargs.get("scene_id")
            player_id = kwargs.get("player_id")
            if campaign_id is not None and not Campaign.objects.filter(
                pk=campaign_id, owner_id=user.pk
            ).exists():
                return HttpResponseNotFound("Not found.")
            if scene_id is not None and not Scene.objects.filter(
                pk=scene_id, campaign__owner_id=user.pk
            ).exists():
                return HttpResponseNotFound("Not found.")
            if player_id is not None and not Player.objects.filter(
                pk=player_id, campaign__owner_id=user.pk
            ).exists():
                return HttpResponseNotFound("Not found.")

        return self.get_response(request)


class PlayerTokenSafetyMiddleware:
    """No caching, URL referrals or browser cross-origin POSTs for bearer links.

    The bearer URL remains the player's credential: POSTs intentionally do not
    require a separate Django session or CSRF cookie. Cross-site browser forms
    must not be able to exercise leaked or guessed bearer links.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path_info.startswith("/play/"):
            return self.get_response(request)
        if request.method not in ("GET", "HEAD", "POST", "OPTIONS"):
            return self._protect(HttpResponseForbidden("Method not allowed."))

        if request.method == "POST":
            origin = request.headers.get("Origin", "")
            fetch_site = request.headers.get("Sec-Fetch-Site", "")
            if fetch_site == "cross-site":
                return self._protect(HttpResponseForbidden("Cross-site POST denied."))
            if origin and not self._same_origin(request, origin):
                return self._protect(HttpResponseForbidden("Cross-origin POST denied."))

        return self._protect(self.get_response(request))

    @staticmethod
    def _same_origin(request, origin):
        from urllib.parse import urlsplit

        parsed = urlsplit(origin)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return False
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False
        # request.get_host() is validated by Django ALLOWED_HOSTS.
        if (parsed.scheme, parsed.netloc.lower()) == (
            "https" if request.is_secure() else "http", request.get_host().lower()
        ):
            return True
        public_host = getattr(settings, "PUBLIC_PLAYER_HOST", "")
        return bool(
            public_host
            and parsed.scheme == "https"
            and parsed.netloc.lower() == public_host
            and request.get_host().split(":", 1)[0].lower() == public_host
        )

    @staticmethod
    def _protect(response):
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        response["Referrer-Policy"] = "no-referrer"
        response["X-Robots-Tag"] = "noindex, nofollow"
        response["X-Content-Type-Options"] = "nosniff"
        return response
