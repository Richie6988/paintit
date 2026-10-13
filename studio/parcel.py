"""Suivi colis international (AfterShip) : on attend que le colis arrive dans le pays du client, puis on lui
envoie le n° du transporteur national (dernier kilometre : La Poste, DHL, PostNL...) avec un e-mail « Ça arrive ! ».

Cycle (cron `supplier_sync`, toutes les 15 min, chaque colis relu au plus toutes les CHECK_EVERY) :
  1. commande expediee avec n° de suivi fournisseur -> enregistrement chez AfterShip (transporteur auto-detecte) ;
  2. lecture : checkpoint dans le pays de destination (ou statut « en livraison / point relais / livre »)
     => arrivee ; n° du dernier kilometre = tracking.last_mile (sinon le n° d'origine) ;
  3. arrivee -> e-mail client (une seule fois) ; livre -> statut « Livree » (+ demande d'avis).
Sans cle AFTERSHIP_API_KEY : rien ne se passe (le reste du parcours est inchange).
"""
import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)
API = "https://api.aftership.com/tracking/2024-10"
CHECK_EVERY = timedelta(hours=4)
GIVE_UP = timedelta(days=60)
ARRIVED_TAGS = {"OutForDelivery", "AvailableForPickup", "Delivered"}
ISO3 = {"FR": "FRA", "BE": "BEL", "CH": "CHE", "LU": "LUX", "DE": "DEU", "ES": "ESP", "IT": "ITA", "PT": "PRT",
        "NL": "NLD", "AT": "AUT", "IE": "IRL", "DK": "DNK", "SE": "SWE", "NO": "NOR", "FI": "FIN", "PL": "POL",
        "CZ": "CZE", "GR": "GRC", "GB": "GBR", "CA": "CAN", "US": "USA", "AU": "AUS", "JP": "JPN", "BR": "BRA",
        "MA": "MAR", "TN": "TUN", "CI": "CIV", "SN": "SEN", "AE": "ARE", "SG": "SGP"}


class ParcelError(Exception):
    pass


def enabled():
    return bool(getattr(settings, "AFTERSHIP_API_KEY", ""))


def _http(method, path, data=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"as-api-key": settings.AFTERSHIP_API_KEY, "Content-Type": "application/json",
                                          "User-Agent": "PaintIt/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        meta = body.get("meta") or {}
        raise ParcelError("AfterShip HTTP %s %s %s" % (e.code, meta.get("code", ""), meta.get("message", "")))
    data = body.get("data") or {}
    return data.get("tracking") or data      # versions d'API : data.tracking ou data


def dest_iso2(order):
    from . import address
    c = order.country or ""
    return ((address.COUNTRIES.get(c) or (c[:2].upper(),))[0] or "").upper()


def _save(order, **kw):
    from .models import Order
    order.parcel = {**(order.parcel or {}), **kw}
    Order.objects.filter(pk=order.pk).update(parcel=order.parcel)


def register(order):
    """Declare le colis a AfterShip. Si deja connu (doublon), on le retrouve par son numero."""
    iso2 = dest_iso2(order)
    body = {"tracking_number": order.tracking_number, "order_id": order.uid, "title": "PaintIt %s" % order.uid,
            "language": (order.lang or "fr")[:2]}
    if ISO3.get(iso2):
        body["destination_country_region"] = ISO3[iso2]
    try:
        t = _http("POST", "/trackings", body)
    except ParcelError as e:
        if "4003" not in str(e):        # 4003 = suivi deja existant
            raise
        found = _http("GET", "/trackings?tracking_numbers=%s" % order.tracking_number)
        lst = found.get("trackings") if isinstance(found, dict) else None
        t = (lst or [{}])[0]
    if not t.get("id"):
        raise ParcelError("AfterShip : identifiant de suivi absent")
    _save(order, id=t["id"], number=order.tracking_number, registered=timezone.now().isoformat(timespec="seconds"))
    return t


def _country(cp):
    c = (cp.get("country_region") or cp.get("country_iso3") or cp.get("country") or "").upper()
    return c


def analyse(order, t):
    """-> dict(state, arrived, delivered, last_mile{carrier,number,url}, checkpoint)"""
    iso2 = dest_iso2(order)
    want = {iso2, ISO3.get(iso2, "")} - {""}
    cps = t.get("checkpoints") or []
    tag = t.get("tag") or ""
    arrived = tag in ARRIVED_TAGS or any(_country(cp) in want for cp in cps)
    lm = t.get("last_mile") or {}
    number = lm.get("tracking_number") or ""
    carrier = lm.get("slug") or ""
    url = lm.get("courier_tracking_link") or lm.get("courier_redirect_link") or ""
    if not number:      # pas de transfert de numero : le n° d'origine est suivi jusqu'au bout
        number, carrier, url = order.tracking_number, t.get("slug") or order.carrier, \
            t.get("courier_tracking_link") or order.tracking_url
    # transporteur du dernier checkpoint dans le pays, a defaut
    local = [cp for cp in cps if _country(cp) in want]
    if local and not lm.get("slug"):
        carrier = local[-1].get("slug") or carrier
    last = cps[-1] if cps else {}
    return {"state": tag, "arrived": arrived, "delivered": tag == "Delivered",
            "last_mile": {"carrier": carrier or "", "number": number or "",
                          "url": url or ("https://t.17track.net/fr#nums=%s" % number if number else "")},
            "checkpoint": ("%s · %s" % (last.get("checkpoint_time", "")[:16], last.get("message", "")))[:200] if last else ""}


def check(order, force=False):
    """Une passe sur une commande. -> texte court (journal cron)."""
    from .models import OrderEvent
    from . import emails
    p = order.parcel or {}
    if not order.tracking_number:
        return "pas de n° de suivi"
    if p.get("number") and p["number"] != order.tracking_number:
        p = {}      # n° change (correction fournisseur) : on recommence
        order.parcel = {}
    if not force and p.get("last_check"):
        from django.utils.dateparse import parse_datetime
        last = parse_datetime(p["last_check"])
        if last and timezone.now() - last < CHECK_EVERY:
            return "deja verifie"
    try:
        if not p.get("id"):
            register(order)
        t = _http("GET", "/trackings/%s" % order.parcel["id"])
    except ParcelError as e:
        _save(order, error=str(e)[:200], last_check=timezone.now().isoformat(timespec="seconds"))
        return "erreur : %s" % e
    a = analyse(order, t)
    upd = {"state": a["state"], "checkpoint": a["checkpoint"], "last_mile": a["last_mile"], "error": "",
           "last_check": timezone.now().isoformat(timespec="seconds")}
    if a["arrived"] and not p.get("dest_arrived_at"):
        upd["dest_arrived_at"] = upd["last_check"]
    _save(order, **upd)
    out = a["state"] or "en transit"
    if a["arrived"] and not order.parcel.get("notified_at"):
        if emails.send_coming_soon(order, a["last_mile"]):
            _save(order, notified_at=timezone.now().isoformat(timespec="seconds"))
            lm = a["last_mile"]
            OrderEvent.objects.create(order=order, kind="action", user="suivi colis",
                                      text=("Arrivé dans le pays : e-mail « ça arrive » envoyé (%s %s)"
                                            % (lm["carrier"], lm["number"]))[:300])
            out += ", client prévenu"
    if a["delivered"] and order.status == order.SHIPPED:
        from .integrations import handle_event
        sup = order.supplier
        if sup:
            handle_event(sup, {"uid": order.uid, "status": "delivered", "message": "Suivi colis : livré"})
            out += ", livré"
    return out


def due_orders():
    from .models import Order
    since = timezone.now() - GIVE_UP
    return (Order.objects.filter(status=Order.SHIPPED, created_at__gte=since).exclude(tracking_number="")
            .order_by("status_changed_at"))
