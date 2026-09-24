from django.apps import AppConfig
class StudioConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "studio"

    def ready(self):
        try:   # photos iPhone (HEIC/HEIF) lisibles par Pillow
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except Exception:
            pass
        self._protect_admin_login()

    @staticmethod
    def _protect_admin_login():
        """Anti force brute sur /admin/login/ : 5 echecs par compte (10 par IP) = blocage 15 min."""
        from django.contrib import admin
        from django.contrib.auth.signals import user_logged_in, user_login_failed
        from django.http import HttpResponse
        from . import security

        orig = admin.site.login

        def login(request, extra_context=None):
            if request.method == "POST" and security.login_blocked(request, request.POST.get("username", "")):
                return HttpResponse("<h1>Trop de tentatives</h1><p>Réessayez dans 15 minutes.</p>", status=429)
            return orig(request, extra_context)

        admin.site.login = login

        def failed(sender, credentials=None, request=None, **kw):
            if request is not None:
                security.record_login_failure(request, (credentials or {}).get("username", ""))

        def logged_in(sender, request=None, user=None, **kw):
            if request is not None:
                request.session.pop("otp_ok", None)   # le 2e facteur est redemande a chaque connexion

        user_login_failed.connect(failed, weak=False, dispatch_uid="paintit_login_failed")
        user_logged_in.connect(logged_in, weak=False, dispatch_uid="paintit_login_ok")
