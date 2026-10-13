from django.conf import settings
from django.conf.urls.static import static
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import path, include, re_path

from studio import woo_api

# Non prefixes par la langue (admin + endpoint de changement de langue)
urlpatterns = [
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
    # Connecteur WooCommerce pour l'ERP fournisseur (MangoERP) : hors i18n, slash final optionnel
    re_path(r"^wc-api/(?P<version>v[123])/?(?P<route>.*)$", woo_api.legacy),
    re_path(r"^wp-json/?(?P<route>.*)$", woo_api.wp_json),
]

# Pages prefixees par langue : /en/, /de/, /es/ ; le francais (defaut) reste a la racine.
# Les URLs francaises existantes ne changent donc pas ; les webhooks/API restent
# accessibles en non-prefixe (ils resolvent sur la langue par defaut sans redirection).
urlpatterns += i18n_patterns(
    path("", include("studio.urls")),
    prefix_default_language=False,
)

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
