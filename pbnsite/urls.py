from django.conf import settings
from django.conf.urls.static import static
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import path, include

# Non prefixes par la langue (admin + endpoint de changement de langue)
urlpatterns = [
    path("admin/", admin.site.urls),
    path("i18n/", include("django.conf.urls.i18n")),
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
