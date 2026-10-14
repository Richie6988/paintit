"""Donnees structurees schema.org (JSON-LD) pour les resultats enrichis Google."""
import json

from django.conf import settings
from django.utils.safestring import mark_safe


def dump(obj):
    """JSON-LD sur pour un <script> (pas de '</' qui fermerait la balise)."""
    return mark_safe(json.dumps(obj, ensure_ascii=False).replace("</", "<\\/"))


def product(request=None, with_reviews=False):
    """Produit + offre agregee (fourchette de prix reelle de la boutique)."""
    from .models import KitFormat, Pricing
    p = Pricing.get()
    fmts = [float(f.price) for f in KitFormat.objects.filter(available=True)] or [p.format_price("40x50")]
    cols = [p.color_price(n) for n in p.available_colors()] or [0.0]
    site = settings.SITE_URL
    data = {
        "@context": "https://schema.org", "@type": "Product",
        "name": "Peinture par numéros personnalisée PaintIt",
        "description": ("Kit de peinture par numéros 100 % personnalisé, calculé à partir de votre photo par un "
                        "algorithme de précision : toile numérotée, peintures numérotées et poster de référence. "
                        "Aperçu et jeu en ligne gratuits avant d'acheter. Dès 4 ans en 12 couleurs."
                        + " Fabriqué dans nos ateliers."),
        "audience": {"@type": "PeopleAudience", "suggestedMinAge": 4},
        "additionalProperty": [{"@type": "PropertyValue", "name": "Nombre de couleurs",
                                "value": ", ".join(str(n) for n in p.available_colors())}],
        "image": site + "/static/studio/img/og.jpg",
        "brand": {"@type": "Brand", "name": "PaintIt"},
        "category": "Arts & Crafts > Painting Kits",
        "offers": {"@type": "AggregateOffer", "priceCurrency": "EUR",
                   "lowPrice": "%.2f" % (min(fmts) + min(cols)), "highPrice": "%.2f" % (max(fmts) + max(cols)),
                   "offerCount": len(fmts) * len(cols), "availability": "https://schema.org/InStock",
                   "url": site + "/create/",
                   "shippingDetails": {"@type": "OfferShippingDetails",
                                       "shippingRate": {"@type": "MonetaryAmount", "value": "0", "currency": "EUR"},
                                       "deliveryTime": {"@type": "ShippingDeliveryTime",
                                                        "handlingTime": {"@type": "QuantitativeValue", "minValue": 1,
                                                                         "maxValue": 3, "unitCode": "DAY"},
                                                        "transitTime": {"@type": "QuantitativeValue",
                                                                        "minValue": p.delivery_days_min,
                                                                        "maxValue": p.delivery_days_max,
                                                                        "unitCode": "DAY"}}},
                   "hasMerchantReturnPolicy": {"@type": "MerchantReturnPolicy",
                                               "returnPolicyCategory": "https://schema.org/MerchantReturnNotPermitted",
                                               "applicableCountry": "FR"}},
    }
    # Etoiles Google : UNIQUEMENT avec de vrais avis clients verifies et publies (jamais de note inventee).
    from .models import Review
    from django.db.models import Avg, Count
    pub = Review.objects.filter(published=True)
    agg = pub.aggregate(avg=Avg("rating"), n=Count("id")) if with_reviews else {"n": 0}
    if agg["n"]:
        data["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": "%.1f" % agg["avg"],
                                   "reviewCount": agg["n"], "bestRating": "5", "worstRating": "1"}
        data["review"] = [{"@type": "Review", "author": {"@type": "Person", "name": r.author},
                           "datePublished": r.created_at.date().isoformat(),
                           "reviewRating": {"@type": "Rating", "ratingValue": r.rating, "bestRating": 5},
                           **({"reviewBody": r.text[:500]} if r.text else {})} for r in pub[:5]]
    return data


def reviews_summary():
    """Avis publies pour l'affichage (Google exige que les avis balises soient visibles sur la page)."""
    from .models import Review
    from django.db.models import Avg, Count
    pub = Review.objects.filter(published=True)
    agg = pub.aggregate(avg=Avg("rating"), n=Count("id"))
    return {"avg": agg["avg"], "n": agg["n"], "items": list(pub.exclude(text="")[:3])} if agg["n"] else None


def origin():
    try:
        from .models import CompanyInfo
        return CompanyInfo.get().production_origin or ""
    except Exception:
        return ""


def faq(items):
    return {"@context": "https://schema.org", "@type": "FAQPage",
            "mainEntity": [{"@type": "Question", "name": it["q"],
                            "acceptedAnswer": {"@type": "Answer", "text": it["a"]}} for it in items]}


def breadcrumb(*crumbs):
    site = settings.SITE_URL
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": n, "item": site + u}
                                for i, (n, u) in enumerate(crumbs)]}


def item_list(models, url_for):
    site = settings.SITE_URL
    return {"@context": "https://schema.org", "@type": "ItemList",
            "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": m.title or "Modèle PaintIt",
                                 "url": site + url_for(m)} for i, m in enumerate(models[:50])]}


def indexnow_key():
    """Cle IndexNow (Bing, Yandex, Seznam, Naver...) : INDEXNOW_KEY ou derivee de SECRET_KEY (stable)."""
    import hashlib
    from django.conf import settings
    return getattr(settings, "INDEXNOW_KEY", "") or hashlib.sha256(("indexnow" + settings.SECRET_KEY).encode()).hexdigest()[:32]


def sitemap_urls():
    import re
    from django.test import RequestFactory
    from .views import sitemap_xml
    return re.findall(r"<loc>([^<]+)</loc>", sitemap_xml(RequestFactory().get("/sitemap.xml")).content.decode())


def indexnow_submit(urls=None):
    """Soumet les URL publiques a IndexNow (un seul envoi partage entre les moteurs participants). -> code HTTP"""
    import json
    import urllib.request
    from urllib.parse import urlparse
    from django.conf import settings
    urls = urls or sitemap_urls()
    host = urlparse(settings.SITE_URL).netloc
    key = indexnow_key()
    body = json.dumps({"host": host, "key": key, "keyLocation": "%s/%s.txt" % (settings.SITE_URL, key),
                       "urlList": urls[:10000]}).encode()
    req = urllib.request.Request("https://api.indexnow.org/indexnow", data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status
