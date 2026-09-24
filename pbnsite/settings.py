import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default="0"):
    return os.environ.get(name, default) == "1"


def env_list(name, default=""):
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


# --- Coeur / securite ---
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

SITE_URL = os.environ.get("SITE_URL", "https://paintit.click").rstrip("/")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS") or (["*"] if DEBUG else [])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS") or (
    [SITE_URL] if SITE_URL.startswith("http") else [])

if not DEBUG:
    # Derriere un proxy TLS (nginx, load balancer)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", "1")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    if SECRET_KEY == "dev-insecure-change-me":
        raise RuntimeError("DJANGO_SECRET_KEY doit etre defini en production.")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "studio",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "studio.middleware.StaffTwoFactorMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# --- Base de donnees : SQLite par defaut, Postgres via DATABASE_URL ---
DATABASES = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
}
if os.environ.get("DATABASE_URL"):
    try:
        import dj_database_url
        DATABASES["default"] = dj_database_url.parse(
            os.environ["DATABASE_URL"], conn_max_age=600, ssl_require=not DEBUG)
    except Exception:
        pass

SESSION_ENGINE = "django.contrib.sessions.backends.db"

# --- Cache : LocMem par defaut, Redis (partage entre workers) via REDIS_URL ---
if os.environ.get("REDIS_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                          "LOCATION": os.environ["REDIS_URL"]}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                          "LOCATION": "paintit"}}

ROOT_URLCONF = "pbnsite.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "django.template.context_processors.i18n",
        "studio.context.assets",
        "studio.context.erp_nav",
    ]},
}]

WSGI_APPLICATION = "pbnsite.wsgi.application"

LANGUAGE_CODE = "fr"
LANGUAGES = [("fr", "Francais"), ("en", "English"), ("de", "Deutsch"), ("es", "Espanol")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": (
        "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage")},
}

DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024  # 25 Mo
FILE_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Alertes : chaque erreur 500 / exception loggee est envoyee par e-mail a ADMINS.
# DJANGO_ADMINS="Prenom:moi@exemple.com,Autre:autre@exemple.com"
ADMINS = [tuple(x.split(":", 1)) if ":" in x else ("Admin", x) for x in env_list("DJANGO_ADMINS")]
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "filters": {"prod": {"()": "django.utils.log.RequireDebugFalse"}},
    "handlers": {"console": {"class": "logging.StreamHandler"},
                 "mail_admins": {"class": "django.utils.log.AdminEmailHandler", "level": "ERROR",
                                 "filters": ["prod"]}},
    "root": {"handlers": ["console", "mail_admins"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
}

# --- Paiement Stripe ---
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# --- Fournisseur d'impression (OEM dropshipping) ---
SUPPLIER_API_URL = os.environ.get("SUPPLIER_API_URL", "")
SUPPLIER_API_TOKEN = os.environ.get("SUPPLIER_API_TOKEN", "")
SUPPLIER_NAME = os.environ.get("SUPPLIER_NAME", "VANCY ARTS (OEM, Yiwu)")
SUPPLIER_ORDER_EMAIL = os.environ.get("SUPPLIER_ORDER_EMAIL", "")

# --- Remise fidelite (QR du poster) ---
PBN_DISCOUNT_URL = os.environ.get("PBN_DISCOUNT_URL", SITE_URL + "/discount")

# --- Pipeline ---
PBN_DPI = int(os.environ.get("PBN_DPI", "90"))
PBN_BRAND = os.environ.get("PBN_BRAND", "PaintIt")

# --- Verification d'adresse (optionnelle) ---
ADDRESS_API_URL = os.environ.get("ADDRESS_API_URL", "")

# --- E-mail ---
EMAIL_BACKEND = os.environ.get("DJANGO_EMAIL_BACKEND",
                               "django.core.mail.backends.smtp.EmailBackend")
EMAIL_TIMEOUT = int(os.environ.get("EMAIL_TIMEOUT", "15"))   # jamais de requete bloquee par un SMTP lent
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.hostinger.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "465"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "contact@paintit.click")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", "0")
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", "1")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "PaintIt <contact@paintit.click>")
SERVER_EMAIL = os.environ.get("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "contact@paintit.click")

# Lien d'avis client (Trustpilot/Judge.me/Google...) pour l'e-mail de feedback.
REVIEW_URL = os.environ.get("REVIEW_URL", "")
# Mesure d'audience sans cookie. Plausible : ANALYTICS_SRC=https://plausible.io/js/script.js
ANALYTICS_SRC = os.environ.get("ANALYTICS_SRC", "")
# 1 = double authentification obligatoire pour tout compte admin (sinon : active compte par compte)
REQUIRE_2FA = env_bool("REQUIRE_2FA", "0")
ANALYTICS_DOMAIN = os.environ.get("ANALYTICS_DOMAIN", "paintit.click")
