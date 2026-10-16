"""Mesure d'audience interne, sans cookie ni stockage dans le navigateur.

- Visiteur : empreinte sha256(sel du jour + IP + navigateur) tronquee, qui change chaque jour (non reversible,
  pas de suivi d'un jour a l'autre) : visiteurs uniques par jour, visite = visiteur x jour.
- Le script de page envoie : vue de page, temps passe (a la sortie), clics sur les appels a l'action.
- Pays : en-tete du proxy (CF-IPCountry / X-Country) si present, sinon region de la langue du navigateur.
- Exclus : robots, equipe connectee (staff), pages admin.
"""
import hashlib
import json
import re
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import F
from django.utils import timezone

BOT_RX = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|monitor|curl|wget|python-requests|facebookexternalhit", re.I)
SKIP_PATHS = ("/admin", "/admin-hub", "/static", "/media", "/a/", "/api/", "/pbn", "/marketing", "/admin-tarifs")


def _salt(day):
    return hashlib.sha256(("%s|%s" % (settings.SECRET_KEY, day.isoformat())).encode()).hexdigest()


def visitor_id(request, day=None):
    from .security import client_ip
    day = day or timezone.localdate()
    raw = "%s|%s|%s" % (_salt(day), client_ip(request), request.META.get("HTTP_USER_AGENT", "")[:300])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_trackable(request):
    ua = request.META.get("HTTP_USER_AGENT", "")
    if not ua or BOT_RX.search(ua):
        return False
    user = getattr(request, "user", None)
    return not (user and user.is_authenticated and user.is_staff)


def country_of(request):
    for h in ("HTTP_CF_IPCOUNTRY", "HTTP_X_COUNTRY", "HTTP_X_GEO_COUNTRY"):
        v = (request.META.get(h) or "").strip().upper()
        if len(v) == 2 and v.isalpha() and v not in ("XX", "T1"):
            return v
    al = request.META.get("HTTP_ACCEPT_LANGUAGE", "")
    m = re.match(r"\s*([a-zA-Z]{2})[-_]([a-zA-Z]{2})", al)
    return m.group(2).upper() if m else ""


def _device(ua, width):
    ua = ua.lower()
    if "ipad" in ua or "tablet" in ua or (width and 600 <= width < 1024 and "mobi" not in ua):
        return "tablet"
    if "mobi" in ua or "android" in ua or (width and width < 600):
        return "mobile"
    return "desktop"


def _clean_path(p):
    p = (p or "/").split("#")[0][:200]
    return p if p.startswith("/") else "/"


def _ref_host(ref, request):
    try:
        host = urlparse(ref or "").netloc.lower()
    except ValueError:
        return ""
    if not host or host == request.get_host().lower():
        return ""
    return host[4:] if host.startswith("www.") else host[:120]


def collect(request):
    """Point d'entree du script de page (POST JSON / sendBeacon). -> dict de reponse."""
    from .models import PageHit, SiteEvent, Visit
    if not is_trackable(request):
        return {}
    try:
        data = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return {}
    now = timezone.now()
    day = timezone.localdate()
    vid = visitor_id(request, day)
    t = data.get("t")
    path = _clean_path(data.get("p"))
    if path.startswith(SKIP_PATHS):
        return {}
    if t == "pv":
        ua = request.META.get("HTTP_USER_AGENT", "")
        q = data.get("q") or ""
        utm = re.search(r"(?:^|[?&])utm_source=([^&]+)", q)
        v, created = Visit.objects.get_or_create(day=day, visitor=vid, defaults=dict(
            first_at=now, last_at=now, entry_path=path, referrer=_ref_host(data.get("r"), request),
            utm_source=(utm.group(1)[:60] if utm else ""), country=country_of(request),
            lang=(request.META.get("HTTP_ACCEPT_LANGUAGE", "")[:2] or "").lower(),
            device=_device(ua, int(data.get("w") or 0))))
        Visit.objects.filter(pk=v.pk).update(pages=F("pages") + 1, last_at=now)
        h = PageHit.objects.create(visit=v, at=now, path=path)
        return {"h": h.pk}
    if t == "end":
        sec = max(0, min(1800, int(data.get("d") or 0)))
        hit = PageHit.objects.filter(pk=data.get("h"), visit__visitor=vid, visit__day=day, duration_s=0).first()
        if hit and sec:
            PageHit.objects.filter(pk=hit.pk).update(duration_s=sec)
            Visit.objects.filter(pk=hit.visit_id).update(duration_s=F("duration_s") + sec, last_at=now)
        return {}
    if t == "c":
        name = re.sub(r"\s+", " ", str(data.get("n") or "")).strip()[:80]
        if name:
            SiteEvent.objects.create(at=now, day=day, visitor=vid, kind="cta", name=name, path=path)
        return {}
    return {}


def log_event(request, kind, name, path=""):
    """Evenement cote serveur (ex. generation d'une toile). Ne leve jamais."""
    from .models import SiteEvent
    try:
        if not is_trackable(request):
            return
        SiteEvent.objects.create(at=timezone.now(), day=timezone.localdate(), visitor=visitor_id(request),
                                 kind=kind, name=name[:80], path=(path or request.path)[:200])
    except Exception:
        pass


# --------------------------------------------------------------------------- tableau de bord
COUNTRIES = {"FR": "France", "BE": "Belgique", "CH": "Suisse", "LU": "Luxembourg", "CA": "Canada", "DE": "Allemagne",
             "AT": "Autriche", "ES": "Espagne", "IT": "Italie", "PT": "Portugal", "NL": "Pays-Bas", "GB": "Royaume-Uni",
             "IE": "Irlande", "US": "États-Unis", "MA": "Maroc", "DZ": "Algérie", "TN": "Tunisie", "SN": "Sénégal",
             "CI": "Côte d'Ivoire", "RE": "La Réunion", "GP": "Guadeloupe", "MQ": "Martinique", "PL": "Pologne",
             "SE": "Suède", "DK": "Danemark", "NO": "Norvège", "FI": "Finlande", "CZ": "Tchéquie", "GR": "Grèce",
             "RO": "Roumanie", "HU": "Hongrie", "MX": "Mexique", "AR": "Argentine", "BR": "Brésil", "CO": "Colombie",
             "CL": "Chili", "AU": "Australie", "NZ": "Nouvelle-Zélande", "IN": "Inde", "CN": "Chine", "JP": "Japon"}
PERIODS = [("1", "Aujourd'hui"), ("7", "7 jours"), ("30", "30 jours"), ("90", "90 jours")]


def flag(cc):
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in cc) if len(cc or "") == 2 and cc.isalpha() else "🌐"


def _pct(a, b):
    return round(a / b * 100, 1) if b else None


def _delta(cur, prev):
    return round((cur - prev) / prev * 100, 1) if prev else None


def _bounds(days):
    import datetime
    today = timezone.localdate()
    start = today - datetime.timedelta(days=days - 1)
    prev_start = start - datetime.timedelta(days=days)
    return start, today, prev_start, start - datetime.timedelta(days=1)


def _totals(a, b):
    from django.db.models import Avg, Count, Q, Sum
    from .models import Order, PageHit, SiteEvent, Visit
    v = Visit.objects.filter(day__gte=a, day__lte=b)
    agg = v.aggregate(n_v=Count("id"), n_p=Sum("pages"), n_b=Count("id", filter=Q(pages__lte=1)),
                      a_d=Avg("duration_s", filter=Q(duration_s__gt=0)))
    agg = {"n": agg["n_v"], "pages": agg["n_p"], "bounce": agg["n_b"], "dur": agg["a_d"]}
    n = agg["n"] or 0
    gens = SiteEvent.objects.filter(kind="generation", day__gte=a, day__lte=b).count()
    orders = Order.real.filter(status__in=Order.PAID_STATUSES, created_at__date__gte=a, created_at__date__lte=b).count()
    return {"visits": n, "pageviews": agg["pages"] or 0, "ppv": round((agg["pages"] or 0) / n, 2) if n else 0,
            "bounce": _pct(agg["bounce"] or 0, n), "duration": int(agg["dur"] or 0), "generations": gens,
            "orders": orders, "conv": _pct(orders, n), "gen_rate": _pct(gens, n),
            "clicks": SiteEvent.objects.filter(kind="cta", day__gte=a, day__lte=b).count()}


def dashboard(days=30):
    import datetime
    from collections import Counter
    from django.db.models import Avg, Count, Q
    from .models import PageHit, SiteEvent, Visit
    a, b, pa, pb = _bounds(days)
    cur, prev = _totals(a, b), _totals(pa, pb)
    cards = []
    for key, label, unit, good_up, hint in (
            ("visits", "Visiteurs uniques", "n", True, "Visiteurs anonymes distincts par jour, additionnés sur la période (sans cookie : un même visiteur revenu un autre jour compte à nouveau)"),
            ("pageviews", "Pages vues", "n", True, ""),
            ("ppv", "Pages / visite", "x", True, ""),
            ("duration", "Temps moyen / visite", "s", True, "Temps où la page est réellement affichée (onglet visible)"),
            ("bounce", "Taux de rebond", "pct", False, "Visites d'une seule page"),
            ("generations", "Générations", "n", True, "Aperçus générés depuis une photo + jeux DigiPaint lancés"),
            ("gen_rate", "Visites → génération", "pct", True, ""),
            ("conv", "Visites → commande", "pct", True, "Commandes payées réelles / visites")):
        c, p = cur[key], prev[key]
        cards.append({"label": label, "value": c, "prev": p, "unit": unit, "good_up": good_up, "hint": hint,
                      "delta": _delta(c or 0, p or 0) if c is not None and p is not None else None})
    v = Visit.objects.filter(day__gte=a, day__lte=b)
    n = cur["visits"] or 1
    # serie quotidienne
    by_day = {r["day"]: r for r in v.values("day").annotate(n=Count("id"))}
    gen_day = dict(SiteEvent.objects.filter(kind="generation", day__gte=a, day__lte=b)
                   .values_list("day").annotate(c=Count("id")))
    series, d = [], a
    while d <= b:
        series.append({"day": d, "visits": by_day.get(d, {}).get("n", 0), "gens": gen_day.get(d, 0)})
        d += datetime.timedelta(days=1)
    top = max([s["visits"] for s in series] + [1])
    step = max(1, -(-len(series) // 7))                         # ~7 dates sous l'axe
    for i, s in enumerate(series):
        s["label"] = s["day"].strftime("%d/%m") if (i % step == 0 or i == len(series) - 1) else ""
        s["show_n"] = s["visits"] > 0 and len(series) <= 31
    for s in series:
        s["h"] = round(s["visits"] / top * 100, 1)
        s["hg"] = round(min(s["gens"], top) / top * 100, 1)

    def table(qs, field, limit=12):
        rows = list(qs.values(field).annotate(n=Count("id")).order_by("-n")[:limit])
        return [{"k": r[field] or "", "n": r["n"], "pct": _pct(r["n"], n)} for r in rows]
    countries = table(v, "country")
    for r in countries:
        r["label"] = "%s %s" % (flag(r["k"]), COUNTRIES.get(r["k"], r["k"] or "Inconnu"))
    sources = []
    for r in table(v.exclude(utm_source=""), "utm_source", 8):
        sources.append({"label": "utm : " + r["k"], "n": r["n"], "pct": r["pct"]})
    for r in table(v.filter(utm_source=""), "referrer", 12):
        sources.append({"label": r["k"] or "Accès direct / e-mail", "n": r["n"], "pct": r["pct"]})
    sources.sort(key=lambda r: -r["n"])
    entries = table(v, "entry_path", 12)
    hits = PageHit.objects.filter(visit__day__gte=a, visit__day__lte=b)
    pages = [{"k": r["path"], "n": r["n"], "visitors": r["u"], "avg": int(r["avg"] or 0)}
             for r in hits.values("path").annotate(n=Count("id"), u=Count("visit", distinct=True),
                                                   avg=Avg("duration_s", filter=Q(duration_s__gt=0))).order_by("-n")[:15]]
    seen = dict(hits.values_list("path").annotate(u=Count("visit", distinct=True)))
    ev = SiteEvent.objects.filter(kind="cta", day__gte=a, day__lte=b)
    ctas = []
    for r in ev.values("name", "path").annotate(n=Count("id"), u=Count("visitor", distinct=True)).order_by("-n")[:20]:
        ctas.append({"name": r["name"], "path": r["path"], "n": r["n"], "u": r["u"],
                     "rate": _pct(r["u"], seen.get(r["path"], 0))})
    gens = [{"k": r["name"], "n": r["n"]} for r in SiteEvent.objects.filter(kind="generation", day__gte=a, day__lte=b)
            .values("name").annotate(n=Count("id")).order_by("-n")]
    devices = table(v, "device", 3)
    langs = table(v, "lang", 8)
    live = Visit.objects.filter(last_at__gte=timezone.now() - datetime.timedelta(minutes=30)).count()
    return {"start": a, "end": b, "cards": cards, "cur": cur, "series": series, "countries": countries,
            "sources": sources[:12], "entries": entries, "pages": pages, "ctas": ctas, "gens": gens,
            "devices": devices, "langs": langs, "live": live, "periods": PERIODS, "days": str(days)}


def purge(months=13):
    """Conservation des mesures d'audience : 13 mois (recommandation CNIL)."""
    import datetime
    from .models import SiteEvent, Visit
    cut = timezone.localdate() - datetime.timedelta(days=months * 30)
    return Visit.objects.filter(day__lt=cut).delete()[0] + SiteEvent.objects.filter(day__lt=cut).delete()[0]
