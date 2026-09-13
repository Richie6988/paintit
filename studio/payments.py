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
        metadata={"uid": order["uid"]},
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": "eur",
                "unit_amount": int(round(float(order["total"]) * 100)),
                "product_data": {"name": order_description(order)},
            },
        }],
        success_url=request.build_absolute_uri(
            reverse("studio:pay_success")) + "?uid=" + order["uid"],
        cancel_url=request.build_absolute_uri(reverse("studio:checkout")),
    )
    return session.url


def verify_webhook(payload, sig_header):
    return stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
