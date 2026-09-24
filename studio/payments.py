"""Paiement via Stripe Checkout (remplace Shopify).

- create_checkout_session : cree une session de paiement hebergee et renvoie l'URL.
- verify_webhook : valide la signature du webhook checkout.session.completed.
Sans STRIPE_SECRET_KEY -> mode demo (pas de paiement, fulfilment immediat cote vue).
"""
from django.conf import settings
from django.urls import reverse

try:
    import stripe
except ImportError:
    stripe = None


def stripe_live():
    return bool(settings.STRIPE_SECRET_KEY and stripe)


def demo_allowed():
    """Mode demo (commande honoree SANS paiement) : uniquement en developpement (DEBUG) et sans Stripe.
    En production, pas de cle Stripe = pas de vente, jamais de livraison gratuite."""
    return bool(settings.DEBUG) and not stripe_live()


def order_description(order):
    base = (f"Peinture par numeros personnalisee, {order['format_label']} "
            f"({order['orientation']}), {order['colors']} couleurs")
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
            "price_data": {
                "currency": "eur",
                "unit_amount": int(round(float(order["total"]) * 100)),
                "product_data": {"name": order_description(order)},
            },
        }],
        success_url=request.build_absolute_uri(
            reverse("studio:pay_success")) + "?uid=" + order["uid"] + "&sid={CHECKOUT_SESSION_ID}",
        cancel_url=request.build_absolute_uri(reverse("studio:checkout")),
    )
    return session.url


def create_digital_session(uid, request, amount_eur=0.99, email=None):
    """Checkout Stripe pour deverrouiller une toile numerique (0,99 EUR)."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment",
        client_reference_id=uid,
        customer_email=email or None,
        metadata={"uid": uid, "kind": "digital", "email": email or ""},
        payment_intent_data={"metadata": {"uid": uid, "kind": "digital"}},
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "eur",
                "unit_amount": int(round(float(amount_eur) * 100)),
                "product_data": {"name": "Toile numerique DigiPaint (%s)" % uid},
            },
        }],
        success_url=request.build_absolute_uri(
            reverse("studio:paint_unlock_success")) + "?uid=" + uid + "&sid={CHECKOUT_SESSION_ID}",
        cancel_url=request.build_absolute_uri(reverse("studio:digipaint", args=[uid])),
    )
    return session.url


def create_gallery_session(uid, request, amount_eur, email=None):
    """Checkout Stripe pour acheter un modele de la galerie."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        mode="payment", client_reference_id=uid, customer_email=email or None,
        metadata={"uid": uid, "kind": "gallery", "email": email or ""},
        payment_intent_data={"metadata": {"uid": uid, "kind": "gallery"}},
        line_items=[{"quantity": 1, "price_data": {"currency": "eur",
            "unit_amount": int(round(float(amount_eur) * 100)),
            "product_data": {"name": "Modele PaintIt (%s)" % uid}}}],
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
        sess = stripe.checkout.Session.retrieve(session_id)
    except Exception:
        return None
    meta = sess.get("metadata") or {}
    if sess.get("payment_status") != "paid":
        return None
    if uid and (meta.get("uid") or sess.get("client_reference_id")) != uid:
        return None
    if kind and meta.get("kind") and meta.get("kind") != kind:
        return None
    return sess


def session_is_paid(session_id):
    """Verifie qu'une session Checkout est bien payee."""
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        sess = stripe.checkout.Session.retrieve(session_id)
        return sess.get("payment_status") == "paid", (sess.get("metadata") or {}).get("uid")
    except Exception:
        return False, None


def verify_webhook(payload, sig_header):
    return stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
