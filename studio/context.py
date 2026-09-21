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
    return {"I18N_VERSION": v, "SITE_URL": settings.SITE_URL}
