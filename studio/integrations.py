"""Integration fournisseurs (plug & play) : envoi standardise, retours automatiques, relances.

SORTANT  (PaintIt -> fournisseur), a chaque commande payee (ou a la demande) :
  - API    : POST JSON sur Supplier.api_url
             en-tetes : Authorization: Bearer <api_key>, X-PaintIt-Event, X-PaintIt-Signature
             (HMAC-SHA256 hex du corps avec Supplier.webhook_secret)
  - E-mail : meme contenu (texte + order.json en piece jointe) a Supplier.email
  Le payload contient `callback_url` : l'adresse ou le fournisseur renvoie ses statuts.

ENTRANT  (fournisseur -> PaintIt) : POST JSON sur /api/suppliers/<webhook_secret>/events/
  {"uid": "...", "status": "in_production|shipped|delivered|failed",
   "carrier": "...", "tracking_number": "...", "tracking_url": "...",
   "supplier_ref": "...", "message": "..."}
  -> met a jour la commande (statut, suivi), journalise, demande d'avis a la livraison.

RELANCES : `python manage.py supplier_sync` (cron toutes les 15 min) retransmet les commandes
  dont la derniere transmission a echoue (max MAX_ATTEMPTS) et celles payees jamais transmises.
"""
import glob
import hashlib
import hmac
import json
import logging
import os

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
FAIL_PREFIX = "Echec transmission"
OK_PREFIX = "Transmise a"

STATUS_MAP = {"in_production": "fulfilled", "accepted": "fulfilled", "shipped": "shipped",
              "delivered": "delivered", "failed": "failed", "cancelled": "failed"}


def callback_url(sup):
    return "%s/api/suppliers/%s/events/" % (settings.SITE_URL, sup.webhook_secret)


def sign(sup, body):
    return hmac.new((sup.webhook_secret or "").encode(), body, hashlib.sha256).hexdigest()


def order_files(sup, uid):
    base = settings.SITE_URL + settings.MEDIA_URL + "orders/%s/" % uid
    files = {name: base + name for name in sup.wanted_files(uid)}
    if getattr(sup, "want_source", False):
        d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
        for sp in glob.glob(os.path.join(d, "%s_source_*" % uid))[:1]:
            files[os.path.basename(sp)] = base + os.path.basename(sp)
    return files


def build_payload(sup, o, shipping, event="order.created"):
    return {
        "event": event, "version": 1, "sent_at": timezone.now().isoformat(timespec="seconds"),
        "supplier": sup.name, "callback_url": callback_url(sup),
        "order": {"uid": o.get("uid"), "format": o.get("format_label"), "orientation": o.get("orientation"),
                  "width_cm": o.get("width_cm"), "height_cm": o.get("height_cm"), "colors": o.get("colors"),
                  "brushes": bool(o.get("brushes")), "total": o.get("total")},
        "shipping": {k: shipping.get(k, "") for k in ("full_name", "email", "phone_code", "phone", "address1",
                                                      "address2", "postal_code", "city", "country")},
        "files": order_files(sup, o.get("uid")) if o.get("uid") else {},
    }


def _send(sup, payload):
    """Envoie le payload par le canal du fournisseur. -> (ok, canal, erreur)"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if sup.integration == "api" and sup.api_url:
        import urllib.request
        req = urllib.request.Request(sup.api_url, data=body, headers={
            "Content-Type": "application/json", "User-Agent": "PaintIt-Integration/1",
            "Authorization": "Bearer " + (sup.api_key or ""), "X-PaintIt-Event": payload["event"],
            "X-PaintIt-Signature": sign(sup, body)})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300, "API", "" if 200 <= r.status < 300 else "HTTP %s" % r.status
        except Exception as exc:
            return False, "API", str(exc)[:300]
    if sup.email:
        from django.core.mail import EmailMessage
        o, s = payload["order"], payload["shipping"]
        lines = ["Evenement : %s" % payload["event"]]
        if o.get("uid"):
            lines += ["", "Commande %s" % o["uid"],
                      "Format : %s (%sx%s cm), %s couleurs%s" % (o["format"], o["width_cm"], o["height_cm"], o["colors"],
                                                                 ", pinceaux" if o["brushes"] else ""),
                      "", "Fichiers :"] + ["- %s" % u for u in payload["files"].values()] + \
                     ["", "Livraison :", s["full_name"], s["address1"], s["address2"],
                      "%s %s (%s)" % (s["postal_code"], s["city"], s["country"]),
                      "Tel : %s %s" % (s["phone_code"], s["phone"])]
        lines += ["", "Statuts / suivi a renvoyer (JSON POST) : %s" % payload["callback_url"]]
        try:
            msg = EmailMessage("PaintIt , %s %s" % (payload["event"], o.get("uid") or ""), "\n".join(lines),
                               settings.DEFAULT_FROM_EMAIL, [sup.email])
            msg.attach("order_%s.json" % (o.get("uid") or "test"), json.dumps(payload, ensure_ascii=False, indent=2),
                       "application/json")
            return msg.send(fail_silently=False) > 0, "e-mail", ""
        except Exception as exc:
            return False, "e-mail", str(exc)[:300]
    return False, "", "Aucun canal configure (e-mail ou URL API manquant)"


def dispatch(o, shipping, sup=None, user="", event="order.created"):
    """Transmet une commande. Trace routage, sante et journal. -> True si transmis."""
    from .models import Order, OrderEvent, Supplier
    sup = sup or Supplier.for_checkout("kit")
    if not sup:
        return False
    ok, channel, err = _send(sup, build_payload(sup, o, shipping, event))
    if not ok:
        logger.warning("Transmission %s -> %s : %s", o.get("uid"), sup.name, err)
    Supplier.objects.filter(pk=sup.pk).update(last_sync_at=timezone.now(), last_sync_ok=ok,
                                              last_sync_error="" if ok else err)
    order = Order.objects.filter(uid=o.get("uid")).first()
    if order:
        Order.objects.filter(pk=order.pk).update(supplier=sup)
        OrderEvent.objects.create(order=order, kind="action", user=user or "",
                                  text=("%s %s (%s)" % (OK_PREFIX, sup.name, channel)) if ok
                                  else ("%s %s : %s" % (FAIL_PREFIX, sup.name, err))[:300])
    return ok


def test_connection(sup, user=""):
    """Envoie un evenement 'test' (sans commande) : verifie URL, cle, e-mail."""
    from .models import Supplier
    payload = build_payload(sup, {}, {}, event="test")
    payload["order"] = {"uid": None, "note": "Test de connexion PaintIt : aucune commande reelle."}
    ok, channel, err = _send(sup, payload)
    Supplier.objects.filter(pk=sup.pk).update(last_sync_at=timezone.now(), last_sync_ok=ok,
                                              last_sync_error="" if ok else err)
    return ok, channel, err


def handle_event(sup, data):
    """Retour du fournisseur -> mise a jour de la commande. -> (http_status, message)"""
    from .models import Order, OrderEvent
    from . import emails
    uid = (data.get("uid") or data.get("order_uid") or "").strip()
    order = Order.objects.filter(uid=uid).first()
    if not order:
        return 404, "commande inconnue"
    if order.supplier_id and order.supplier_id != sup.pk:
        return 403, "commande d'un autre fournisseur"
    who = "API %s" % sup.name
    changed = []
    for f in ("carrier", "tracking_number", "tracking_url"):
        v = (data.get(f) or "").strip()
        if v and getattr(order, f) != v:
            setattr(order, f, v[:300]); changed.append(f)
    ref = (data.get("supplier_ref") or "").strip()
    if ref and order.supplier_ref != ref:
        order.supplier_ref = ref[:64]; changed.append("supplier_ref")
    new = STATUS_MAP.get((data.get("status") or "").strip().lower())
    if new and new != order.status:
        order.status = new; changed.append("status")
    if not order.supplier_id:
        order.supplier = sup; changed.append("supplier")
    if changed:
        order._erp_user = who
        fields = changed + (["status_changed_at"] if "status" in changed else [])
        order.save(update_fields=fields)
    msg = (data.get("message") or "").strip()
    OrderEvent.objects.create(order=order, kind="action", user=who, text=(
        "Retour fournisseur : %s%s%s" % (data.get("status") or "maj",
                                         (" · suivi %s %s" % (order.carrier, order.tracking_number))
                                         if "tracking_number" in changed else "",
                                         (" · " + msg) if msg else ""))[:300])
    if order.status == Order.DELIVERED and not order.feedback_sent:
        try:
            emails.send_feedback_request(order)
            Order.objects.filter(pk=order.pk).update(feedback_sent=True)
        except Exception:
            logger.exception("Avis apres livraison %s", uid)
    return 200, "ok"


def pending_retries():
    """Commandes dont la DERNIERE transmission a echoue (moins de MAX_ATTEMPTS essais)."""
    from .models import Order, OrderEvent
    out = []
    for o in Order.objects.filter(status__in=(Order.PAID, Order.FULFILLED)):
        evs = list(OrderEvent.objects.filter(order=o, kind="action", text__regex=r"^(%s|%s)" % (FAIL_PREFIX, OK_PREFIX))
                   .order_by("-at").values_list("text", flat=True)[:MAX_ATTEMPTS + 1])
        if evs and evs[0].startswith(FAIL_PREFIX):
            fails = 0
            for t in evs:
                if not t.startswith(FAIL_PREFIX):
                    break
                fails += 1
            if fails < MAX_ATTEMPTS:
                out.append(o)
    return out
