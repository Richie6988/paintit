"""ERP PaintIt : indicateurs de pilotage, centre d'actions (SLA) et vue clients.

Definitions (uniques, partagees par toutes les vues staff) :
  - CA encaisse = somme des `total` des commandes payees (Order.PAID_STATUSES),
    les commandes en attente de paiement ou en echec ne comptent pas ;
  - marge brute = CA - cout fournisseur estime (Order.cost) ;
  - periode = [debut, maintenant], comparee a la periode precedente de meme duree.
"""
import datetime
from collections import OrderedDict

from django.db.models import Count, Max, Min, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone

from .models import ContactMessage, DigitalCanvas, Discount, Order, Pricing, Supplier

LIB = "__library__"
PERIODS = OrderedDict([("7d", ("7 jours", 7)), ("30d", ("30 jours", 30)), ("90d", ("90 jours", 90)),
                       ("12m", ("12 mois", 365)), ("ytd", ("Année en cours", None))])


def period_bounds(key):
    now = timezone.now()
    key = key if key in PERIODS else "30d"
    days = PERIODS[key][1]
    if days is None:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = (now - datetime.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    span = now - start
    return key, start, now, start - span, start


def _paid():
    return Order.objects.filter(status__in=Order.PAID_STATUSES)


def _sales(start, end):
    qs = _paid().filter(created_at__gte=start, created_at__lt=end)
    a = qs.aggregate(ca=Sum("total"), cost=Sum("cost"), n=Count("id"), disc=Sum("discount_amount"),
                     customers=Count("customer_email", distinct=True))
    ca = a["ca"] or 0.0
    cost = a["cost"] or 0.0
    n = a["n"] or 0
    created = Order.objects.filter(created_at__gte=start, created_at__lt=end).count()
    # clients nouveaux = premiere commande payee dans la periode
    firsts = (_paid().values("customer_email").annotate(first=Min("created_at"))
              .filter(first__gte=start, first__lt=end).count())
    gal = gallery_sales(start, end)
    return {"revenue": round(ca, 2), "margin": round(ca - cost, 2),
            "margin_pct": round((ca - cost) / ca * 100, 1) if ca else 0.0,
            "orders": n, "aov": round(ca / n, 2) if n else 0.0,
            "discounts": round(a["disc"] or 0.0, 2), "customers": a["customers"] or 0,
            "new_customers": firsts, "created": created,
            "pay_rate": round(n / created * 100, 1) if created else 0.0,
            "gallery_n": gal["n"], "gallery_revenue": gal["revenue"],
            "canvases": DigitalCanvas.objects.exclude(email=LIB).filter(
                source="digital", created_at__gte=start, created_at__lt=end).count()}


def gallery_sales(start=None, end=None):
    """Ventes de modeles payants de la galerie : une ligne DigitalCanvas joueur (source=library)
    par achat ; prix = prix actuel du modele en galerie (estimation)."""
    prices = {u: float(p) for u, p in DigitalCanvas.objects.filter(email=LIB, price__gt=0)
              .values_list("uid", "price")}
    qs = DigitalCanvas.objects.filter(source="library", uid__in=list(prices)).exclude(email=LIB)
    if start:
        qs = qs.filter(created_at__gte=start, created_at__lt=end)
    uids = list(qs.values_list("uid", flat=True))
    return {"n": len(uids), "revenue": round(sum(prices[u] for u in uids), 2)}


def _delta(cur, prev):
    if not prev:
        return None if not cur else 100.0
    return round((cur - prev) / abs(prev) * 100, 1)


def kpis(period="30d"):
    key, start, end, pstart, pend = period_bounds(period)
    cur, prev = _sales(start, end), _sales(pstart, pend)
    cards = [
        ("revenue", "CA encaissé", "eur", "Commandes payées (hors attente / échec)"),
        ("margin", "Marge brute", "eur", "CA - coût fournisseur estimé"),
        ("orders", "Commandes payées", "int", ""),
        ("aov", "Panier moyen", "eur", ""),
        ("new_customers", "Nouveaux clients", "int", "1re commande payée sur la période"),
        ("pay_rate", "Taux de paiement", "pct", "Payées / commandes créées"),
        ("gallery_revenue", "Ventes galerie", "eur", "Modeles payants (prix actuel)"),
        ("discounts", "Remises accordées", "eur", ""),
    ]
    out = []
    for k, label, unit, hint in cards:
        out.append({"key": k, "label": label, "unit": unit, "hint": hint, "value": cur[k],
                    "prev": prev[k], "delta": _delta(cur[k], prev[k]),
                    # une hausse des remises n'est pas une bonne nouvelle
                    "good_up": k != "discounts"})
    return {"period": key, "period_label": PERIODS[key][0], "start": start, "cards": out,
            "cur": cur, "prev": prev,
            "periods": [(k, v[0]) for k, v in PERIODS.items()]}


def series(period="30d"):
    """CA / marge / commandes par jour (<= 90 j) ou par mois."""
    key, start, end, _ps, _pe = period_bounds(period)
    by_month = (end - start).days > 92
    trunc = TruncMonth if by_month else TruncDate
    rows = (_paid().filter(created_at__gte=start).annotate(b=trunc("created_at")).values("b")
            .annotate(ca=Sum("total"), cost=Sum("cost"), n=Count("id")))
    data = {}
    for r in rows:
        b = r["b"].date() if hasattr(r["b"], "date") and by_month else r["b"]
        data[b] = r
    labels, ca, margin, n = [], [], [], []
    if by_month:
        d = start.date().replace(day=1)
        while d <= end.date():
            r = data.get(d, {})
            labels.append(d.strftime("%m/%Y")); ca.append(round(r.get("ca") or 0, 2))
            margin.append(round((r.get("ca") or 0) - (r.get("cost") or 0), 2)); n.append(r.get("n") or 0)
            d = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    else:
        d = start.date()
        while d <= end.date():
            r = data.get(d, {})
            labels.append(d.strftime("%d/%m")); ca.append(round(r.get("ca") or 0, 2))
            margin.append(round((r.get("ca") or 0) - (r.get("cost") or 0), 2)); n.append(r.get("n") or 0)
            d += datetime.timedelta(days=1)
    return {"labels": labels, "revenue": ca, "margin": margin, "orders": n}


def mix(period="30d"):
    _k, start, _e, _ps, _pe = period_bounds(period)
    qs = _paid().filter(created_at__gte=start)

    def top(field, limit=8):
        return [{"label": r[field] or "—", "n": r["n"], "ca": round(r["ca"] or 0, 2)}
                for r in qs.values(field).annotate(n=Count("id"), ca=Sum("total")).order_by("-ca")[:limit]]
    return {"formats": top("format_label"), "colors": top("colors"), "countries": top("country"),
            "brushes_rate": round(qs.filter(brushes=True).count() / qs.count() * 100, 1) if qs.exists() else 0}


def pipeline():
    labels = dict(Order.STATUS_CHOICES)
    counts = dict(Order.objects.values_list("status").annotate(c=Count("id")))
    order = [Order.PENDING, Order.PAID, Order.FULFILLED, Order.SHIPPED, Order.DELIVERED, Order.FAILED]
    return [{"status": s, "label": labels[s], "count": counts.get(s, 0),
             "url": "/admin-hub/orders/?tab=" + s} for s in order]


# --------------------------------------------------------------------------- #
# Centre d'actions : ce qui demande une intervention, avec SLA
# --------------------------------------------------------------------------- #
def _lead_days():
    s = Supplier.for_checkout("kit")
    return int(s.lead_time_days) if s else 5


def action_querysets():
    """{cle: (queryset Order, libelle, niveau, consigne)} , reutilise par le filtre admin."""
    now = timezone.now()
    p = Pricing.get()
    lead = _lead_days()
    ship_max = int(p.delivery_days_max or 9)
    return OrderedDict([
        ("failed", (Order.objects.filter(status=Order.FAILED),
                    "Commandes en échec", "err", "Vérifier le paiement / le fournisseur puis relancer")),
        ("to_supplier", (Order.objects.filter(status=Order.PAID, status_changed_at__lt=now - datetime.timedelta(hours=12)),
                         "Payées non transmises au fournisseur (> 12 h)", "err",
                         "Générer les fichiers et transmettre la commande")),
        ("no_tracking", (Order.objects.filter(status=Order.FULFILLED, tracking_number="",
                                              status_changed_at__lt=now - datetime.timedelta(days=lead)),
                         "En production sans n° de suivi (> %d j)" % lead, "warn",
                         "Relancer le fournisseur et saisir le suivi colis")),
        ("late_delivery", (Order.objects.filter(status=Order.SHIPPED,
                                                status_changed_at__lt=now - datetime.timedelta(days=ship_max + 3)),
                           "Expédiées non livrées (> %d j)" % (ship_max + 3), "warn",
                           "Vérifier le suivi transporteur, prévenir le client")),
        ("abandoned", (Order.objects.filter(status=Order.PENDING,
                                            created_at__lt=now - datetime.timedelta(hours=24),
                                            created_at__gte=now - datetime.timedelta(days=30)),
                       "Paiements abandonnés (24 h - 30 j)", "info", "Relancer le client (panier abandonné)")),
        ("no_feedback", (Order.objects.filter(status=Order.DELIVERED, feedback_sent=False),
                         "Livrées sans demande d'avis", "info", "Envoyer la demande d'avis")),
    ])


def actions():
    now = timezone.now()
    out = []
    for key, (qs, label, level, hint) in action_querysets().items():
        n = qs.count()
        if n:
            out.append({"key": key, "label": label, "level": level, "hint": hint, "count": n,
                        "url": "/admin-hub/orders/?action=" + key})
    late_msgs = ContactMessage.objects.filter(answered=False, created_at__lt=now - datetime.timedelta(hours=24)).count()
    unanswered = ContactMessage.objects.filter(answered=False).count()
    if unanswered:
        out.append({"key": "messages", "count": unanswered, "hint": "Répondre depuis la fiche message",
                    "label": "Messages sans réponse" + (" (dont %d > 24 h)" % late_msgs if late_msgs else ""),
                    "level": "err" if late_msgs else "warn",
                    "url": "/admin/studio/contactmessage/?answered__exact=0"})
    if not Supplier.for_checkout("kit"):
        out.append({"key": "supplier", "count": "!", "level": "err", "url": "/admin/studio/supplier/add/",
                    "label": "Aucun fournisseur actif pour le checkout Kit",
                    "hint": "Les commandes ne peuvent pas être transmises"})
    promos = Discount.objects.filter(kind=Discount.PROMO, active=True, max_uses__gt=0)
    exhausted = [d.pk for d in promos if d.used_count >= d.max_uses]
    if exhausted:
        out.append({"key": "promos", "count": len(exhausted), "level": "info",
                    "url": "/admin/studio/discount/?kind__exact=promo&active__exact=1",
                    "label": "Codes promo épuisés encore actifs", "hint": "Désactiver ou augmenter le quota"})
    rank = {"err": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda a: rank[a["level"]])


def action_count():
    """Total pour le badge de navigation."""
    return sum(a["count"] for a in actions() if isinstance(a["count"], int))


# --------------------------------------------------------------------------- #
# Clients (agreges depuis les commandes, cle = e-mail)
# --------------------------------------------------------------------------- #
def customers(q="", sort="-revenue", segment=""):
    qs = Order.objects.exclude(customer_email="")
    if q:
        qs = qs.filter(Q(customer_email__icontains=q) | Q(customer_name__icontains=q) | Q(city__icontains=q))
    rows = (qs.values("customer_email")
            .annotate(orders=Count("id", filter=Q(status__in=Order.PAID_STATUSES)),
                      attempts=Count("id"),
                      revenue=Sum("total", filter=Q(status__in=Order.PAID_STATUSES)),
                      cost=Sum("cost", filter=Q(status__in=Order.PAID_STATUSES)),
                      first=Min("created_at"), last=Max("created_at"),
                      name=Max("customer_name"), country=Max("country"), city=Max("city")))
    now = timezone.now()
    out = []
    for r in rows:
        rev = r["revenue"] or 0.0
        seg = ("vip" if rev >= 150 or r["orders"] >= 3 else "fidele" if r["orders"] >= 2
               else "client" if r["orders"] == 1 else "prospect")
        if r["orders"] and (now - r["last"]).days > 180:
            seg = "inactif"
        out.append(dict(r, revenue=round(rev, 2), margin=round(rev - (r["cost"] or 0), 2),
                        segment=seg, days=(now - r["last"]).days,
                        canvases=0))
    emails = [r["customer_email"] for r in out]
    cv = dict(DigitalCanvas.objects.filter(email__in=emails).values_list("email").annotate(c=Count("id")))
    for r in out:
        r["canvases"] = cv.get(r["customer_email"], 0)
    if segment:
        out = [r for r in out if r["segment"] == segment]
    key = sort.lstrip("-")
    if key in ("revenue", "orders", "last", "margin", "days", "customer_email"):
        out.sort(key=lambda r: (r[key] is None, r[key]), reverse=sort.startswith("-"))
    return out


def customer_stats():
    rows = customers()
    buyers = [r for r in rows if r["orders"]]
    repeat = [r for r in buyers if r["orders"] >= 2]
    rev = sum(r["revenue"] for r in buyers)
    segs = OrderedDict((s, 0) for s in ("vip", "fidele", "client", "inactif", "prospect"))
    for r in rows:
        segs[r["segment"]] += 1
    return {"total": len(rows), "buyers": len(buyers),
            "repeat_rate": round(len(repeat) / len(buyers) * 100, 1) if buyers else 0.0,
            "ltv": round(rev / len(buyers), 2) if buyers else 0.0, "segments": segs,
            "top": sorted(buyers, key=lambda r: -r["revenue"])[:8]}


# --------------------------------------------------------------------------- #
# Fournisseurs : performance, file de production, sante de l'integration
# --------------------------------------------------------------------------- #
def _production_days(order_ids):
    """{order_id: jours entre 'en production' et 'expediee'} d'apres le journal."""
    from .models import OrderEvent
    first = {}
    for e in (OrderEvent.objects.filter(order_id__in=order_ids, kind="status",
                                        status__in=[Order.FULFILLED, Order.SHIPPED])
              .order_by("at").values("order_id", "status", "at")):
        first.setdefault((e["order_id"], e["status"]), e["at"])
    out = {}
    for oid in order_ids:
        a, b = first.get((oid, Order.FULFILLED)), first.get((oid, Order.SHIPPED))
        if a and b and b >= a:
            out[oid] = (b - a).total_seconds() / 86400
    return out


def supplier_stats(sup, days=30):
    now = timezone.now()
    since = now - datetime.timedelta(days=days)
    p = Pricing.get()
    lead = int(sup.lead_time_days or 5)
    ship_max = int(p.delivery_days_max or 9)
    qs = Order.objects.filter(supplier=sup)
    recent = qs.filter(created_at__gte=since, status__in=Order.PAID_STATUSES)
    a = recent.aggregate(n=Count("id"), cost=Sum("cost"), ca=Sum("total"))
    late_prod = qs.filter(status=Order.FULFILLED, status_changed_at__lt=now - datetime.timedelta(days=lead))
    late_ship = qs.filter(status=Order.SHIPPED, status_changed_at__lt=now - datetime.timedelta(days=ship_max + 3))
    shipped_ids = list(qs.filter(status__in=(Order.SHIPPED, Order.DELIVERED)).values_list("id", flat=True)[:500])
    prod = _production_days(shipped_ids)
    avg = round(sum(prod.values()) / len(prod), 1) if prod else None
    on_time = round(sum(1 for d in prod.values() if d <= lead) / len(prod) * 100) if prod else None
    return {"orders_30": a["n"] or 0, "spend_30": round(a["cost"] or 0, 2), "revenue_30": round(a["ca"] or 0, 2),
            "orders_total": qs.count(), "in_production": qs.filter(status=Order.FULFILLED).count(),
            "shipped": qs.filter(status=Order.SHIPPED).count(),
            "late": late_prod.count() + late_ship.count(), "late_prod": late_prod.count(),
            "late_ship": late_ship.count(), "avg_days": avg, "lead": lead, "on_time": on_time,
            "failed": qs.filter(status=Order.FAILED).count()}


def unassigned_orders():
    """Commandes payees sans fournisseur rattache (avant ce suivi, ou transmission impossible)."""
    return Order.objects.filter(supplier__isnull=True, status__in=(Order.PAID, Order.FULFILLED))
