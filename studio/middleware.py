"""Double authentification obligatoire pour les pages d'administration une fois activee sur le compte."""
import re

from django.conf import settings
from django.shortcuts import redirect

PROTECTED = ("/admin", "/admin-hub", "/admin-tarifs", "/marketing", "/dashboard", "/pbn")
OPEN = ("/admin-hub/2fa", "/admin/login", "/admin/logout", "/admin/jsi18n")
_LANG = re.compile(r"^/(%s)(?=/)" % "|".join(code for code, _ in settings.LANGUAGES))


class StaffTwoFactorMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = _LANG.sub("", request.path)
        user = getattr(request, "user", None)
        if (user is not None and user.is_authenticated and user.is_staff
                and path.startswith(PROTECTED) and not path.startswith(OPEN)):
            from .models import StaffTOTP
            has = StaffTOTP.objects.filter(user=user).exists()
            if has and request.session.get("otp_ok") != user.pk:
                return redirect("/admin-hub/2fa/?next=" + request.get_full_path())
            if not has and getattr(settings, "REQUIRE_2FA", False):
                return redirect("/admin-hub/2fa/setup/")
        return self.get_response(request)
