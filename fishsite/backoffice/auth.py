from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.signals import user_login_failed
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.cache import add_never_cache_headers
from django.views.decorators.debug import sensitive_post_parameters
from axes.handlers.proxy import AxesProxyHandler
from django_otp import user_has_device
from two_factor.forms import AuthenticationTokenForm, BackupTokenForm
from two_factor.views import (
    BackupTokensView,
    LoginView,
    ProfileView,
    QRGeneratorView,
    SetupCompleteView,
    SetupView,
)


class StaffAuthenticationForm(AuthenticationForm):
    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        super().__init__(request=request, *args, **kwargs)

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not user.is_staff:
            raise ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": user.get_username()},
            )


class StaffOTPFormMixin:
    def __init__(self, user, initial_device, request=None, **kwargs):
        self.request = request
        super().__init__(user=user, initial_device=initial_device, **kwargs)

    def clean(self):
        credentials = {"username": self.user.get_username()}
        if self.request is not None and not AxesProxyHandler.is_allowed(self.request, credentials):
            self.request.axes_locked_out = True
            self.request.axes_credentials = credentials
            raise ValidationError("Слишком много попыток входа. Попробуйте позже.")
        if not self.user.is_active or not self.user.is_staff:
            raise ValidationError("Доступ к панели управления отключён.")
        try:
            cleaned_data = super().clean()
        except ValidationError:
            if self.request is not None:
                user_login_failed.send(
                    sender=type(self),
                    request=self.request,
                    credentials=credentials,
                )
            raise
        return cleaned_data


class StaffAuthenticationTokenForm(StaffOTPFormMixin, AuthenticationTokenForm):
    pass


class StaffBackupTokenForm(StaffOTPFormMixin, BackupTokenForm):
    pass


@method_decorator(sensitive_post_parameters(), name="dispatch")
class StaffLoginView(LoginView):
    form_list = (
        (LoginView.AUTH_STEP, StaffAuthenticationForm),
        (LoginView.TOKEN_STEP, StaffAuthenticationTokenForm),
        (LoginView.BACKUP_STEP, StaffBackupTokenForm),
    )

    def done(self, form_list, **kwargs):
        response = super().done(form_list, **kwargs)
        if getattr(self.get_user(), "otp_device", None) is None:
            return redirect("two_factor:setup")
        return response


def _staff_view(view, *, verified=False, enrollment=False):
    @login_required(login_url=settings.LOGIN_URL)
    @wraps(view)
    def checked(request, *args, **kwargs):
        user = request.user
        if not user.is_active or not user.is_staff:
            raise PermissionDenied
        if (verified or (enrollment and user_has_device(user))) and not user.is_verified():
            return redirect(reverse("two_factor:login"))
        response = view(request, *args, **kwargs)
        add_never_cache_headers(response)
        response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return response

    return checked


app_name = "two_factor"
urlpatterns = [
    # Keep the official names and paths so django-two-factor-auth and the
    # admin can reverse their normal profile/setup links.
    path("account/login/", StaffLoginView.as_view(), name="login"),
    path("account/two_factor/setup/", _staff_view(SetupView.as_view(), enrollment=True), name="setup"),
    path("account/two_factor/qrcode/", _staff_view(QRGeneratorView.as_view(), enrollment=True), name="qr"),
    path("account/two_factor/setup/complete/", _staff_view(SetupCompleteView.as_view(), verified=True), name="setup_complete"),
    path("account/two_factor/backup/tokens/", _staff_view(BackupTokensView.as_view(), verified=True), name="backup_tokens"),
    path("account/two_factor/", _staff_view(ProfileView.as_view(), verified=True), name="profile"),
]
