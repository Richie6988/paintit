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
