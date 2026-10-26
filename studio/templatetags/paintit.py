from django import template

register = template.Library()


@register.simple_tag
def paintit_kpis():
    from studio.models import Order, ContactMessage, Discount
    rows = list(Order.objects.filter(status=Order.FULFILLED))
    t = sum(o.total for o in rows)
    c = sum(o.cost for o in rows)
    return {
        "orders": len(rows),
        "total_orders": Order.objects.count(),
        "turnover": round(t, 2), "cost": round(c, 2), "benefit": round(t - c, 2),
        "pending": Order.objects.filter(status=Order.PENDING).count(),
        "unanswered": ContactMessage.objects.filter(answered=False).count(),
        "discounts": Discount.objects.filter(active=True).count(),
    }


@register.simple_tag(takes_context=True)
def money(context, eur):
    """Prix catalogue en EUR -> affiche dans la devise du visiteur (meme arrondi que le paiement Stripe)."""
    from django.utils.translation import get_language
    from studio import currency as fx
    cur = context.get("CUR") or "EUR"
    return fx.fmt(fx.local(eur, cur), cur, get_language())


@register.simple_tag(takes_context=True)
def money_local(context, amount, cur=None):
    """Montant deja exprime dans la devise (recapitulatif de commande)."""
    from django.utils.translation import get_language
    from studio import currency as fx
    return fx.fmt(amount, cur or context.get("CUR") or "EUR", get_language())


@register.filter
def safe_json(value):
    """Valeur Python -> litteral JSON sur pour un <script>."""
    import json
    from django.utils.safestring import mark_safe
    return mark_safe(json.dumps(value).replace("<", "\\u003c"))
