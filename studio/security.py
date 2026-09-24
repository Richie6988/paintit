"""Petits utilitaires de securite : liens de fichiers signes, codes non devinables, limitation de debit."""
import hashlib
import hmac
import secrets

from django.conf import settings
from django.core.cache import cache


def _sig(*parts):
    msg = "|".join(parts).encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def file_token(uid, name):
    return _sig("file", uid, name)[:32]


def file_url(uid, name, absolute=True):
    """Lien de telechargement signe d'un fichier de media/orders/<uid>/ (dossier NON public)."""
    path = "/files/%s/%s?t=%s" % (uid, name, file_token(uid, name))
    return (settings.SITE_URL + path) if absolute else path


def check_file_token(uid, name, token):
    return bool(token) and hmac.compare_digest(file_token(uid, name), token)


def referral_code_for(uid):
    """Code de parrainage d'une commande : derive de l'uid mais impossible a inverser
    (l'uid donne acces aux fichiers de la commande, il ne doit pas circuler)."""
    return (_sig("referral", uid)[:10] + "-R").upper()


def random_code(digits=6):
    return "%0*d" % (digits, secrets.randbelow(10 ** digits))


def rate_limited(key, limit, window):
    """True si `key` a deja atteint `limit` hits sur `window` secondes (et compte ce hit sinon)."""
    k = "rl:" + key
    try:
        n = cache.get(k, 0)
        if n >= limit:
            return True
        if n == 0:
            cache.set(k, 1, window)
        else:
            cache.incr(k)
    except Exception:
        return False
    return False


def client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (fwd.split(",")[0].strip() if fwd else "") or request.META.get("REMOTE_ADDR", "")
