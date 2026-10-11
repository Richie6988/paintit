"""Donnees structurees schema.org (JSON-LD) pour les resultats enrichis Google."""
import json

from django.conf import settings
from django.utils.safestring import mark_safe


def dump(obj):
    """JSON-LD sur pour un <script> (pas de '</' qui fermerait la balise)."""
    return mark_safe(json.dumps(obj, ensure_ascii=False).replace("</", "<\\/"))


def product(request=None):
    """Produit + offre agregee (fourchette de prix reelle de la boutique)."""
    from .models import KitFormat, Pricing
    p = Pricing.get()
    fmts = [float(f.price) for f in KitFormat.objects.filter(available=True)] or [p.format_price("40x50")]
    cols = [p.color_price(n) for n in p.available_colors()] or [0.0]
    site = settings.SITE_URL
    return {
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
