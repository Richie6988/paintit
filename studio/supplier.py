"""Commande fournisseur OEM (dropshipping), a partir de order.json.

Canaux par priorite : API (SUPPLIER_API_URL) > e-mail PO (SUPPLIER_ORDER_EMAIL) > demo.
Le bon de commande EST order.json (source unique). Pieces jointes e-mail : SVG, poster, order.json.
"""
import json
import os

from django.conf import settings
from django.core.mail import EmailMessage
from .models import Pricing

try:
    import requests
except ImportError:
    requests = None


def estimate_cost(order):
    p = Pricing.get()
    area = float(order["width_cm"]) * float(order["height_cm"])
    c = (p.cost_base + area * p.cost_per_cm2
         + int(order["colors"]) * p.cost_per_color + p.cost_shipping)
    return round(c, 2)


def _order_json(uid):
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    with open(os.path.join(d, "order.json"), encoding="utf-8") as f:
        return json.load(f), d


def _email_po(order_data, d, uid):
    body = ("Nouvelle commande dropshipping PaintIt (details en piece jointe order.json).\n\n"
            + json.dumps({k: order_data[k] for k in ("uid", "product", "customer", "print")},
                         ensure_ascii=False, indent=2))
    msg = EmailMessage(subject=f"[PaintIt] Commande {uid}", body=body,
                       from_email=settings.DEFAULT_FROM_EMAIL, to=[settings.SUPPLIER_ORDER_EMAIL])
    for fn in ("order.json", f"{uid}_template.svg", f"{uid}_poster.png"):
        p = os.path.join(d, fn)
        if os.path.exists(p):
            msg.attach_file(p)
    msg.send(fail_silently=True)


def place_order(order, shipping, manifest=None):
    uid = order["uid"]
    data, d = _order_json(uid)
    if settings.SUPPLIER_API_URL and requests:
        headers = {"Authorization": f"Bearer {settings.SUPPLIER_API_TOKEN}"} if settings.SUPPLIER_API_TOKEN else {}
        r = requests.post(settings.SUPPLIER_API_URL, json=data, headers=headers, timeout=20)
        r.raise_for_status()
        resp = r.json()
        return {"status": "sent", "supplier_ref": resp.get("id") or resp.get("reference")}
    if settings.SUPPLIER_ORDER_EMAIL:
        _email_po(data, d, uid)
        return {"status": "emailed", "supplier_ref": "PO-" + uid}
    return {"status": "demo", "supplier_ref": "SUP-" + uid}
