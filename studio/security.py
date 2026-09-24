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


# ---------------- Admin : anti force brute + double authentification (TOTP) ----------------
LOGIN_MAX_FAILS, LOGIN_WINDOW = 5, 900


def login_blocked(request, username=""):
    ip = client_ip(request)
    try:
        return (cache.get("lf-ip:" + ip, 0) >= LOGIN_MAX_FAILS * 2
                or (username and cache.get("lf-u:" + username.lower(), 0) >= LOGIN_MAX_FAILS))
    except Exception:
        return False


def record_login_failure(request, username=""):
    for k in ("lf-ip:" + client_ip(request), "lf-u:" + (username or "").lower()):
        try:
            if cache.get(k) is None:
                cache.set(k, 1, LOGIN_WINDOW)
            else:
                cache.incr(k)
        except Exception:
            pass


def totp_secret():
    import base64
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret, step=None):
    import base64
    import struct
    import time
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int(time.time() // 30) if step is None else step
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def totp_verify(secret, code):
    import time
    code = (code or "").replace(" ", "")
    now = int(time.time() // 30)
    return bool(secret) and len(code) == 6 and any(
        hmac.compare_digest(totp_code(secret, now + d), code) for d in (-1, 0, 1))


def totp_uri(secret, username):
    from urllib.parse import quote
    return "otpauth://totp/PaintIt:%s?secret=%s&issuer=PaintIt" % (quote(username), secret)
