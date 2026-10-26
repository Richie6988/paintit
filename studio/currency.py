"""Prix en devise locale : un visiteur des Etats-Unis voit (et paie) des dollars, du Royaume-Uni des livres...

- Devise choisie a la 1re visite d'apres le pays (en-tete du proxy, sinon langue du navigateur), puis gardee en
  session : elle ne change pas au milieu d'une commande. Robots / moteurs de recherche : toujours EUR.
- Taux BCE du jour (mis en cache 12 h, dernier taux connu garde en secours), puis arrondi « psychologique » :
  49,90 EUR -> 54,90 USD. Le prix affiche est EXACTEMENT celui debite par Stripe.
- La compta reste en EUR : les montants encaisses en devise y sont convertis au taux du jour.
"""
import json
import logging
import math
import os
import re
import urllib.request

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# code -> (symbole, symbole avant le montant ?)
CURRENCIES = {"EUR": ("€", False), "USD": ("$", True), "GBP": ("£", True), "NZD": ("NZ$", True)}
COUNTRY_CURRENCY = {"US": "USD", "GB": "GBP", "NZ": "NZD"}
DEFAULT_RATES = {"EUR": 1.0, "USD": 1.08, "GBP": 0.85, "NZD": 1.80}     # secours si la BCE est injoignable
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
CACHE_KEY, SESSION_KEY = "fx:ecb", "cur"


def _fallback_path():
    return os.path.join(settings.MEDIA_ROOT, "private", "fx.json")


def rates():
    """{code: unites pour 1 EUR}. BCE (cache 12 h) > dernier taux enregistre > valeurs par defaut."""
    r = cache.get(CACHE_KEY)
    if r:
        return r
    r = None
    try:
        with urllib.request.urlopen(urllib.request.Request(ECB_URL, headers={"User-Agent": "PaintIt/1"}),
                                    timeout=8) as f:
            xml = f.read().decode("utf-8", "replace")
        got = {c: float(v) for c, v in re.findall(r"currency='([A-Z]{3})'\s+rate='([\d.]+)'", xml)}
        if all(c in got for c in CURRENCIES if c != "EUR"):
            r = {"EUR": 1.0, **{c: got[c] for c in CURRENCIES if c != "EUR"}}
            try:
                os.makedirs(os.path.dirname(_fallback_path()), exist_ok=True)
                with open(_fallback_path(), "w") as fh:
                    json.dump(r, fh)
            except OSError:
                pass
    except Exception as exc:
        logger.warning("Taux BCE indisponibles : %s", exc)
    if not r:
        try:
            with open(_fallback_path()) as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            r = dict(DEFAULT_RATES)
    cache.set(CACHE_KEY, r, 12 * 3600)
    return r


def psych(v):
    """Arrondi commercial vers le haut : 53,17 -> 53,90 ; petits montants : 1,08 -> 1,09."""
    if v >= 5:
        return round(math.ceil(v - 0.9 - 1e-9) + 0.9, 2)
    return round(math.ceil(round((v + 0.01) * 10, 6)) / 10 - 0.01, 2)


def local(eur, cur):
    """Prix catalogue EUR -> prix affiche ET debite dans la devise `cur`."""
    eur = float(eur or 0)
    if cur == "EUR" or cur not in CURRENCIES or eur <= 0:
        return round(eur, 2)
    return psych(eur * rates()[cur])


def to_eur(amount, cur):
    """Montant encaisse en devise -> EUR (compta, controles)."""
    if not cur or cur.upper() == "EUR":
        return round(float(amount or 0), 2)
    return round(float(amount or 0) / rates().get(cur.upper(), 1.0), 2)


def fmt(amount, cur, lang="fr"):
    sym, before = CURRENCIES.get(cur, ("€", False))
    s = "%.2f" % float(amount or 0)
    if not (before or (lang or "fr")[:2] == "en"):
        s = s.replace(".", ",")
    return ("%s%s" % (sym, s)) if before else ("%s %s" % (s, sym))


def for_request(request):
    """Devise du visiteur (gardee en session). `?cur=USD` permet de la changer."""
    if request is None:
        return "EUR"
    want = (request.GET.get("cur") or "").upper()
    if want in CURRENCIES:
        request.session[SESSION_KEY] = want
        return want
    cur = request.session.get(SESSION_KEY) if hasattr(request, "session") else None
    if cur in CURRENCIES:
        return cur
    from .analytics import BOT_RX, country_of
    ua = request.META.get("HTTP_USER_AGENT", "")
    if not ua or BOT_RX.search(ua):
        return "EUR"                       # moteurs de recherche : prix de reference en euros
    cur = COUNTRY_CURRENCY.get(country_of(request), "EUR")
    if hasattr(request, "session"):
        request.session[SESSION_KEY] = cur
    return cur


def order_amounts(order, cur):
    """Montants d'une commande kit dans la devise du client (memes regles a l'affichage et chez Stripe).
    toile (cadre compris) et pinceaux convertis chacun, remise en % appliquee sur le sous-total local."""
    canvas_eur = float(order.get("canvas_price") or (float(order["price"]) - float(order.get("brushes_amount") or 0)))
    canvas = local(canvas_eur, cur)
    brushes = local(order.get("brushes_amount") or 0, cur) if order.get("brushes") else 0.0
    sub = round(canvas + brushes, 2)
    d = order.get("discount") or None
    rate = float(d.get("rate") or 0) if d else 0.0
    disc = round(sub * rate, 2)
    return {"cur": cur, "canvas": canvas, "brushes": brushes, "subtotal": sub, "discount": disc,
            "total": round(sub - disc, 2)}
