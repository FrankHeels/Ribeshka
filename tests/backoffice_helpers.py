"""Authentication helpers for protected Django Admin test clients."""

from django.test import RequestFactory
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice


def login_verified_admin(client, user):
    """Log in a staff user with a confirmed synthetic TOTP device in session."""
    device = TOTPDevice.objects.create(
        user=user,
        name=f"Test authenticator {user.pk}",
        confirmed=True,
    )
    client.force_login(user)
    request = RequestFactory().get("/")
    request.user = user
    request.session = client.session
    otp_login(request, device)
    request.session.save()
    return device
