"""Security helpers shared by authentication and proxy boundaries."""


def client_ip_from_remote_addr(request):
    # The reverse proxy must set the connection source address correctly.
    # Do not trust user-controlled X-Forwarded-For / X-Real-IP headers.
    return request.META.get("REMOTE_ADDR")
