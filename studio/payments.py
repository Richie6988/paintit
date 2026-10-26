"""Paiement via Stripe Checkout (remplace Shopify).

- create_checkout_session : cree une session de paiement hebergee et renvoie l'URL.
- verify_webhook : valide la signature du webhook checkout.session.completed.
Sans STRIPE_SECRET_KEY -> mode demo (pas de paiement, fulfilment immediat cote vue).
"""
from django.conf import settings
from django.urls import reverse
from django.utils.translation import get_language

try:
    import stripe
except ImportError:
    stripe = None


def plain(obj):
    """Objet Stripe -> dict/list Python natifs (stripe-python >= 12 n'expose plus .get() sur ses objets)."""
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [plain(x) for x in obj]
    return obj


def stripe_live():
    return bool(settings.STRIPE_SECRET_KEY and stripe)


def demo_allowed():
    """Mode demo (commande honoree SANS paiement) : uniquement en developpement (DEBUG) et sans Stripe.
    En production, pas de cle Stripe = pas de vente, jamais de livraison gratuite."""
    return bool(settings.DEBUG) and not stripe_live()


def order_description(order):
    base = (f"Peinture par numeros personnalisee, {order['format_label']} "
            f"({order['orientation']}), {order['colors']} couleurs")
    if order.get("framed"):
        base += ", avec cadre"
    if order.get("brushes"):
        base += " + pinceaux"
    return base


def create_checkout_session(order, shipping, request):
    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment",
        client_reference_id=order["uid"],
        customer_email=shipping.get("email"),
        metadata={"uid": order["uid"], "kind": "kit"},
        payment_intent_data={"metadata": {"uid": order["uid"], "kind": "kit"}},
        line_items=[{
            "quantity": 1,
            "price_data": {      # devise du client : exactement le montant affiche au recapitulatif
                "currency": (order.get("currency") or "EUR").lower(),
                "unit_amount": int(round(float((order.get("amounts") or {}).get("total", order["total"])) * 100)),
                "product_data": {"name": order_description(order)},
            },
        }],
        success_url=request.build_absolute_uri(
            reverse("studio:pay_success")) + "?uid=" + order["uid"] + "&sid={CHECKOUT_SESSION_ID}",
        cancel_url=request.build_absolute_uri(reverse("studio:checkout")),
    )
    return session.url


def create_digital_session(uid, request, amount_eur=0.99, email=None, download=False):
    """Checkout Stripe pour deverrouiller une toile numerique (0,99 EUR, ou equivalent dans la devise du client)."""
    from .currency import for_request, local
    cur = for_request(request)
    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment",
        client_reference_id=uid,
        customer_email=email or None,
        metadata={"uid": uid, "kind": "digital", "email": email or "", "lang": (get_language() or "fr")[:2]},
        payment_intent_data={"metadata": {"uid": uid, "kind": "digital"}},
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": cur.lower(),
                "unit_amount": int(round(local(amount_eur, cur) * 100)),
                "product_data": {"name": "Toile numérique PaintIt (%s)" % uid},
            },
        }],
        success_url=request.build_absolute_uri(
            reverse("studio:paint_unlock_success")) + "?uid=" + uid + ("&dl=1" if download else "")
            + "&sid={CHECKOUT_SESSION_ID}",
        cancel_url=request.build_absolute_uri(reverse("studio:digipaint", args=[uid])),
    )
    return session.url


def create_gallery_session(uid, request, amount_eur, email=None, title=""):
    """Checkout Stripe pour acheter un modele de la galerie."""
    from .currency import for_request, local
    cur = for_request(request)
    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment", client_reference_id=uid, customer_email=email or None,
        metadata={"uid": uid, "kind": "gallery", "email": email or "", "lang": (get_language() or "fr")[:2]},
        payment_intent_data={"metadata": {"uid": uid, "kind": "gallery"}},
        line_items=[{"quantity": 1, "price_data": {"currency": cur.lower(),
            "unit_amount": int(round(local(amount_eur, cur) * 100)),
            "product_data": {"name": ("Toile numérique PaintIt « %s »" % title) if title else
                             "Toile numérique PaintIt (%s)" % uid,
                             "description": "Modèle à peindre en ligne, ajouté à « Mes toiles » (%s)" % uid}}}],
        success_url=request.build_absolute_uri(reverse("studio:gallery_buy_success"))
                    + "?uid=" + uid + "&sid={CHECKOUT_SESSION_ID}",
        cancel_url=request.build_absolute_uri(reverse("studio:gallery")))
    return session.url


def paid_session(session_id, uid=None, kind=None):
    """Session Checkout PAYEE (et conforme a uid/kind attendus), sinon None."""
    if not (session_id and stripe_live()):
        return None
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        sess = plain(stripe.checkout.Session.retrieve(session_id))
    except Exception:
        return None
    meta = sess.get("metadata") or {}
    if sess.get("payment_status") != "paid":
        return None
    if uid and (meta.get("uid") or sess.get("client_reference_id")) != uid:
        return None
    if kind and meta.get("kind") and meta.get("kind") != kind:
        return None
    from . import ledger
    ledger.record_from_session(sess)        # grand livre (idempotent : le webhook enregistre aussi)
    return sess


def session_is_paid(session_id):
    """Verifie qu'une session Checkout est bien payee."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        sess = plain(stripe.checkout.Session.retrieve(session_id))
        return sess.get("payment_status") == "paid", (sess.get("metadata") or {}).get("uid")
    except Exception:
        return False, None


def verify_webhook(payload, sig_header):
    return plain(stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET))
