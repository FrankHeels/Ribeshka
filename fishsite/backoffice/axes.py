from ipaddress import ip_address

from django.conf import settings


def get_attempt_username(request, credentials=None):
    """Read the account name from Axes credentials or the two-factor wizard form."""
    credentials = credentials or {}
    return (
        credentials.get("username")
        or request.POST.get("auth-username")
        or request.POST.get("username")
        or ""
    )


def get_attempt_ip(request):
    """Share the public forms' explicitly configured reverse-proxy boundary."""
    remote = request.META.get("REMOTE_ADDR", "")
    if settings.TRUST_X_REAL_IP:
        remote = request.META.get("HTTP_X_REAL_IP") or remote
    try:
        return str(ip_address(remote.strip()))
    except ValueError:
        return None
