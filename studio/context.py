import os
from django.conf import settings


def assets(request):
    """Version de i18n.js (hash du mtime/taille) pour le cache-busting."""
    v = "0"
    for base in (getattr(settings, "STATIC_ROOT", None), os.path.join(settings.BASE_DIR, "studio", "static")):
        if not base:
            continue
        p = os.path.join(base, "studio", "i18n.js")
        if os.path.exists(p):
            st = os.stat(p)
            v = "%x%x" % (int(st.st_mtime), st.st_size)
            break
    site = settings.SITE_URL
    alts = []
    lang_urls = {}
    try:
        from django.urls import translate_url
        for code, _lbl in settings.LANGUAGES:
            rel = translate_url(request.path, code)
            lang_urls[code] = rel
            alts.append({"lang": code, "href": site + rel})
        alts.append({"lang": "x-default", "href": site + translate_url(request.path, settings.LANGUAGE_CODE)})
    except Exception:
        alts = []
        lang_urls = {}
    native = {"fr": "Français", "en": "English", "de": "Deutsch", "es": "Español", "it": "Italiano"}
    flags = {"fr": "🇫🇷", "en": "🇬🇧", "de": "🇩🇪", "es": "🇪🇸", "it": "🇮🇹"}
    lang_menu = [{"code": c, "label": native.get(c, c), "flag": flags.get(c, ""), "url": lang_urls.get(c, "/")}
                 for c, _l in settings.LANGUAGES]
    return {"I18N_VERSION": v, "SITE_URL": site, "HREFLANGS": alts, "LANG_URLS": lang_urls, "LANG_MENU": lang_menu,
            # Mesure d'audience sans cookie (Plausible / compatible) : pas de bandeau cookies requis
            "ANALYTICS_SRC": getattr(settings, "ANALYTICS_SRC", ""),
            "ANALYTICS_DOMAIN": getattr(settings, "ANALYTICS_DOMAIN", "")}


def erp_nav(request):
    """Badges du menu ERP (admin uniquement, staff connecte)."""
    path = request.path
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and user.is_staff):
        return {}
    if not (path.startswith("/admin") or path.startswith("/admin-")):
        return {}
    try:
        from . import erp
        from .models import ContactMessage, CompanyInfo
        return {"erp_badges": {"actions": erp.action_count(),
                               "messages": ContactMessage.objects.filter(answered=False).count(),
                               "legal": not CompanyInfo.get().complete,
                               "has_2fa": hasattr(request.user, "totp")},
                "erp_path": path}
    except Exception:
        return {}
