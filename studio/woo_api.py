"""Connecteur compatible WooCommerce : l'ERP du fournisseur (MangoERP, 万里牛, ECCANG...) lie paintit.click
comme une boutique WooCommerce et vient y chercher les commandes, sans Shopify ni abonnement.

Cote ERP (MangoERP : 授权 → WooCommerce) : URL du site https://paintit.click + consumerKey/consumerSecret
generes dans la fiche fournisseur (permission lecture/ecriture).

Routes (hors i18n, sans CSRF) :
  API legacy   /wc-api/v1|v2|v3/                 index boutique
               /wc-api/v3/orders                 liste  (status, page, filter[limit|offset|created_at_min|updated_at_min])
               /wc-api/v3/orders/count|statuses
               /wc-api/v3/orders/<id>            GET / PUT|POST (statut, suivi)
               /wc-api/v3/orders/<id>/notes      GET / POST (n° de suivi dans la note)
  API REST     /wp-json/  /wp-json/wc/v3/orders[/<id>[/notes]]  (meme logique, format moderne)
               /wp-json/wc-shipment-tracking/v3/orders/<id>/shipment-trackings
Authentification : Basic (HTTPS), consumer_key/consumer_secret en query, ou OAuth 1.0a (HMAC-SHA1/256).
Seules les commandes payees transmises a CE fournisseur sont exposees ; aucun prix d'achat, aucune marge.
Chaque appel est journalise (ConnectorLog) pour ajuster le format au premier echange reel.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone as dt_tz
from urllib.parse import quote, unquote

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

STATUS_OUT = {"paid": "processing", "fulfilled": "processing", "production": "processing", "shipped": "completed",
              "delivered": "completed", "failed": "cancelled"}
WOO_STATUSES = {"pending": "Pending payment", "processing": "Processing", "on-hold": "On hold",
                "completed": "Completed", "cancelled": "Cancelled", "refunded": "Refunded", "failed": "Failed"}
LOG_KEEP = 300


# ---------------------------------------------------------------- cles
def generate_keys(sup):
    sup.woo = {**(sup.woo or {}), "ck": "ck_" + secrets.token_hex(20), "cs": "cs_" + secrets.token_hex(20),
               "created": timezone.now().isoformat(timespec="seconds")}
    sup.save(update_fields=["woo"])
    return sup.woo


def revoke_keys(sup):
    sup.woo = {k: v for k, v in (sup.woo or {}).items() if k not in ("ck", "cs")}
    sup.save(update_fields=["woo"])


def _find(ck):
    from .models import Supplier
    if not ck or not ck.startswith("ck_"):
        return None
    for sup in Supplier.objects.filter(woo__has_key="ck"):
        if hmac.compare_digest((sup.woo or {}).get("ck", ""), ck):
            return sup
    return None


def _oauth_ok(request, sup, params):
    """OAuth 1.0a one-legged (API Woo sur HTTP) : accepte HMAC-SHA1/256, cle `secret` ou `secret&`."""
    sig = params.get("oauth_signature", "")
    method = (params.get("oauth_signature_method") or "HMAC-SHA1").upper()
    algo = {"HMAC-SHA1": hashlib.sha1, "HMAC-SHA256": hashlib.sha256}.get(method)
    try:
        ts = int(params.get("oauth_timestamp") or 0)
    except ValueError:
        return False
    if not sig or not algo or abs(time.time() - ts) > 900:
        return False
    enc = lambda s: quote(str(s), safe="~")
    norm = "&".join("%s=%s" % (enc(k), enc(v)) for k, v in sorted(
        (unquote(k), unquote(v)) for k, v in params.items() if k != "oauth_signature"))
    cs = sup.woo.get("cs", "")
    for scheme in ("https", "http"):
        base_uri = "%s://%s%s" % (scheme, request.get_host(), request.path)
        for uri in (base_uri, base_uri.rstrip("/")):
            base = "&".join([request.method.upper(), enc(uri), enc(norm)])
            for key in (cs + "&", cs):
                calc = base64.b64encode(hmac.new(key.encode(), base.encode(), algo).digest()).decode()
                if hmac.compare_digest(calc, unquote(sig)) or hmac.compare_digest(calc, sig):
                    return True
    return False


def authenticate(request):
    """-> fournisseur ou None."""
    auth = request.META.get("HTTP_AUTHORIZATION", "")
    ck = cs = ""
    if auth.lower().startswith("basic "):
        try:
            ck, _, cs = base64.b64decode(auth[6:].strip()).decode().partition(":")
        except Exception:
            return None
    elif request.GET.get("consumer_key"):
        ck, cs = request.GET.get("consumer_key", ""), request.GET.get("consumer_secret", "")
    params = {k: v for k, v in request.GET.items()}
    if auth.lower().startswith("oauth "):
        params.update({k: v.strip('"') for k, v in re.findall(r'(oauth_\w+)="?([^",]*)"?', auth)})
    sup = _find(ck or params.get("oauth_consumer_key", ""))
    if not sup or not (sup.active or (sup.woo or {}).get("test")):     # boutique test : inactive mais lisible
        return None
    if cs:
        return sup if hmac.compare_digest(sup.woo.get("cs", ""), cs) else None
    return sup if _oauth_ok(request, sup, params) else None


# ---------------------------------------------------------------- format commande
def _iso(dt, modern=False):
    if not dt:
        return None
    s = dt.astimezone(dt_tz.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return s if modern else s + "Z"


def _updated(o):
    return max(o.created_at, o.status_changed_at or o.created_at)


def _address(o):
    from . import address
    from .order_lines import _split_name, _phone
    first, last = _split_name(o.customer_name)
    iso = (address.COUNTRIES.get(o.country) or (o.country[:2].upper() if o.country else "FR",))[0]
    return {"first_name": first, "last_name": last, "company": "", "address_1": o.address1 or "",
            "address_2": o.address2 or "", "city": o.city or "", "state": "", "postcode": o.postal_code or "",
            "country": iso, "email": o.customer_email, "phone": _phone(o.phone_code, o.phone) or ""}


def _lines(o):
    """Lignes vues par l'ERP : SKU identiques a l'Excel (WF-40x50-24C / BK-...), proprietes = fichiers.
    Pas de ligne pinceaux : ils ne sont pas commandes au fournisseur."""
    from .models import format_key
    from .order_lines import sku_for, PRODUCT_TITLE, variant_title, line_properties, supplier_amounts
    canvas, brush_amt, _total = supplier_amounts(o)          # prix d'achat fournisseur, jamais notre prix de vente
    from .security import file_url
    from .fulfillment import ensure_supplier_files
    ensure_supplier_files(o.uid)
    card = os.path.exists(os.path.join(settings.MEDIA_ROOT, "orders", o.uid, "%s_supplier_card.jpg" % o.uid))
    # image de la ligne = fiche « ALL FILES / 全部文件 » (apercu + QR + lien) : la seule chose que l'ERP affiche a coup sur
    img = file_url(o.uid, ("%s_supplier_card.jpg" if card else "%s_poster.png") % o.uid)
    out = [{"id": o.pk * 10 + 1, "image": img, "sku": sku_for(format_key(o.width_cm, o.height_cm), o.colors, o.finish),
            "name": "%s - %s" % (PRODUCT_TITLE, variant_title(o)), "quantity": 1, "price": canvas,
            "total": canvas, "meta": line_properties(o)}]
    return out


def _note(o):
    from .order_lines import _short_note
    return _short_note(o)


def order_legacy(o):
    a = _address(o)
    lines = _lines(o)
    ship = {k: v for k, v in a.items() if k != "email"}
    return {
        "id": o.pk, "order_number": o.uid, "order_key": "wc_order_" + o.uid,
        "created_at": _iso(o.created_at), "updated_at": _iso(_updated(o)),
        "completed_at": _iso(o.status_changed_at) if o.status in ("shipped", "delivered") else None,
        "status": STATUS_OUT.get(o.status, "pending"), "currency": "EUR",
        "total": "%.2f" % sum(li["total"] for li in lines), "subtotal": "%.2f" % sum(li["total"] for li in lines),
        "total_line_items_quantity": len(lines), "total_tax": "0.00", "total_shipping": "0.00",
        "cart_tax": "0.00", "shipping_tax": "0.00", "total_discount": "0.00",
        "shipping_methods": "Livraison offerte",
        "payment_details": {"method_id": "stripe", "method_title": "Carte bancaire (Stripe)", "paid": True},
        "billing_address": a, "shipping_address": ship, "note": _note(o), "customer_note": _note(o),
        "customer_ip": "", "customer_user_agent": "", "customer_id": 0, "view_order_url": "",
        "line_items": [{"id": li["id"], "subtotal": "%.2f" % li["total"], "subtotal_tax": "0.00",
                        "total": "%.2f" % li["total"], "total_tax": "0.00", "price": "%.2f" % li["price"],
                        "quantity": li["quantity"], "tax_class": "", "name": li["name"], "product_id": li["id"],
                        "image": {"id": li["id"], "src": li["image"]}, "sku": li["sku"], "meta": [{"key": k, "label": k, "value": v} for k, v in li["meta"]]}
                       for li in lines],
        "shipping_lines": [{"id": o.pk, "method_id": "free_shipping", "method_title": "Livraison offerte", "total": "0.00"}],
        "tax_lines": [], "fee_lines": [], "coupon_lines": [],
        "customer": {"id": 0, "email": o.customer_email, "first_name": a["first_name"], "last_name": a["last_name"],
                     "billing_address": a, "shipping_address": ship},
    }


def order_rest(o):
    a = _address(o)
    lines = _lines(o)
    ship = {k: v for k, v in a.items() if k != "email"}
    return {
        "id": o.pk, "parent_id": 0, "number": o.uid, "order_key": "wc_order_" + o.uid, "created_via": "checkout",
        "version": "8.0.0", "status": STATUS_OUT.get(o.status, "pending"), "currency": "EUR",
        "date_created": _iso(o.created_at, True), "date_created_gmt": _iso(o.created_at, True),
        "date_modified": _iso(_updated(o), True), "date_modified_gmt": _iso(_updated(o), True),
        "date_paid": _iso(o.created_at, True), "date_paid_gmt": _iso(o.created_at, True),
        "date_completed": _iso(o.status_changed_at, True) if o.status in ("shipped", "delivered") else None,
        "discount_total": "0.00", "discount_tax": "0.00",
        "shipping_total": "0.00", "shipping_tax": "0.00", "cart_tax": "0.00",
        "total": "%.2f" % sum(li["total"] for li in lines), "total_tax": "0.00", "prices_include_tax": True,
        "customer_id": 0, "customer_note": _note(o), "billing": a, "shipping": ship,
        "payment_method": "stripe", "payment_method_title": "Carte bancaire (Stripe)", "transaction_id": "",
        "meta_data": [{"id": 1, "key": "paintit_uid", "value": o.uid}] + (
            [{"id": 2, "key": "_tracking_number", "value": o.tracking_number},
             {"id": 3, "key": "_tracking_provider", "value": o.carrier}] if o.tracking_number else []),
        "line_items": [{"id": li["id"], "name": li["name"], "product_id": li["id"], "variation_id": 0,
                        "image": {"id": li["id"], "src": li["image"]},
                        "quantity": li["quantity"], "tax_class": "", "subtotal": "%.2f" % li["total"],
                        "subtotal_tax": "0.00", "total": "%.2f" % li["total"], "total_tax": "0.00", "taxes": [],
                        "sku": li["sku"], "price": li["price"],
                        "meta_data": [{"id": i + 1, "key": k, "value": v, "display_key": k, "display_value": v}
                                      for i, (k, v) in enumerate(li["meta"])]} for li in lines],
        "tax_lines": [], "fee_lines": [], "coupon_lines": [], "refunds": [],
        "shipping_lines": [{"id": o.pk, "method_title": "Livraison offerte", "method_id": "free_shipping",
                            "total": "0.00", "total_tax": "0.00", "taxes": [], "meta_data": []}],
    }


# ---------------------------------------------------------------- requetes
def _scope(sup):
    """Commandes de ce fournisseur effectivement transmises via ce connecteur (pas les anciennes envoyees
    par e-mail, ni les commandes de test annulees) : l'ERP ne voit que ce qu'il doit produire."""
    from .models import Order, OrderEvent
    from .integrations import OK_PREFIX, WOO_CHANNEL
    sent = OrderEvent.objects.filter(kind="action", text__startswith=OK_PREFIX, text__contains=WOO_CHANNEL,
                                     order__supplier=sup).values("order_id")
    return Order.objects.filter(supplier=sup, pk__in=sent, status__in=Order.PAID_STATUSES + (Order.FAILED,))


def _parse_dt(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt_tz.utc)


def _single(sup, ref):
    """Commande demandee une a une par l'ERP (lecture, envoi du suivi) : id numerique OU numero de commande.
    Toute commande payee rattachee a CE fournisseur (l'ERP la connait deja). -> (commande, raison si refus)."""
    from .models import Order
    if ref.isdigit():
        o = Order.objects.filter(pk=int(ref)).first()
    elif re.fullmatch(r"[A-Za-z0-9-]{4,40}", ref):
        o = Order.objects.filter(uid__iexact=ref).first()
    else:
        return None, "id invalide"
    if not o:
        return None, "commande inconnue (%s)" % ref[:40]
    if o.supplier_id != sup.pk:
        return None, "commande rattachée à un autre fournisseur (%s)" % (o.supplier.name if o.supplier else "aucun")
    if o.status not in Order.PAID_STATUSES + (Order.FAILED,):
        return None, "commande au statut « %s »" % o.get_status_display()
    return o, ""


def _filter(request, sup, modern):
    from django.db.models import Q
    g = request.GET
    qs = _scope(sup)
    status = (g.get("status") or g.get("filter[status]") or "any").lower()
    if status != "any":
        want = {s.strip() for s in status.split(",")}
        qs = qs.filter(status__in=[k for k, v in STATUS_OUT.items() if v in want])
    for key, field in (("filter[created_at_min]", "after"), ("filter[updated_at_min]", "modified_after")):
        d = _parse_dt(g.get(key) or g.get(field))
        if d and field == "after":
            qs = qs.filter(created_at__gte=d)
        elif d:
            qs = qs.filter(Q(created_at__gte=d) | Q(status_changed_at__gte=d))
    for key, field in (("filter[created_at_max]", "before"), ("filter[updated_at_max]", "modified_before")):
        d = _parse_dt(g.get(key) or g.get(field))
        if d:
            qs = qs.filter(created_at__lte=d)
    inc = g.get("include") or g.get("filter[include]")
    if inc:
        qs = qs.filter(pk__in=[int(x) for x in re.findall(r"\d+", inc)])
    asc = (g.get("order") or g.get("filter[order]") or "desc").lower() == "asc"
    qs = qs.order_by(("" if asc else "-") + "created_at", "pk")
    try:
        limit = int(g.get("per_page") or g.get("filter[limit]") or 10)
        page = max(1, int(g.get("page") or 1))
        offset = int(g.get("offset") or g.get("filter[offset]") or 0)
    except ValueError:
        limit, page, offset = 10, 1, 0
    limit = 100 if limit == -1 else max(1, min(limit, 100))
    total = qs.count()
    start = offset or (page - 1) * limit
    return list(qs[start:start + limit]), total, max(1, -(-total // max(limit, 1)))


def _tracking_from(data, text=""):
    """Extrait transporteur / n° / lien depuis un PUT d'ERP (meta TrackingMore, Shipment Tracking...) ou une note."""
    out = {}
    metas = list(data.get("meta_data") or []) + [{"key": k, "value": v} for k, v in (data.get("order_meta") or {}).items()]
    for m in metas:
        k, v = str(m.get("key", "")).lower(), m.get("value")
        if isinstance(v, list) and v and isinstance(v[0], dict):     # _wc_shipment_tracking_items
            v0 = v[0]
            out.setdefault("tracking_number", str(v0.get("tracking_number") or ""))
            out.setdefault("carrier", str(v0.get("tracking_provider") or v0.get("custom_tracking_provider") or ""))
            out.setdefault("tracking_url", str(v0.get("custom_tracking_link") or ""))
            continue
        if "track" not in k or not v:
            continue
        if "number" in k or k.endswith("tracking_code") or k == "_tracking":
            out.setdefault("tracking_number", str(v))
        elif "provider" in k or "carrier" in k or "courier" in k:
            out.setdefault("carrier", str(v))
        elif "url" in k or "link" in k:
            out.setdefault("tracking_url", str(v))
    for k in ("tracking_number", "tracking_provider", "custom_tracking_provider", "custom_tracking_link"):
        if data.get(k):
            out.setdefault({"tracking_provider": "carrier", "custom_tracking_provider": "carrier",
                            "custom_tracking_link": "tracking_url"}.get(k, k), str(data[k]))
    if text and not out.get("tracking_number") and re.search(r"track|运单|物流|单号|shipp|expédi|colis", text, re.I):
        url = re.search(r"https?://\S+", text)
        if url:
            out.setdefault("tracking_url", url.group(0).rstrip(".,)"))
        cands = [t for t in re.findall(r"\b[A-Z0-9]{8,35}\b", re.sub(r"https?://\S+", " ", text.upper()))
                 if re.search(r"\d", t)]
        if cands:
            out["tracking_number"] = max(cands, key=len)
        car = re.search(r"(?:carrier|courier|provider|transporteur|物流商)\s*[:：]\s*([^\n,;]+)", text, re.I)
        if car:
            out.setdefault("carrier", car.group(1).strip())
    return {k: v.strip()[:300] for k, v in out.items() if v and v.strip()}


def _apply(sup, order, status, trk, note):
    """Statut Woo + suivi -> notre modele (handle_event). Annulation : signalee, pas appliquee."""
    from .integrations import handle_event
    from .models import OrderEvent
    status = (status or "").lower().replace("wc-", "")
    if status in ("cancelled", "refunded", "failed"):
        OrderEvent.objects.create(order=order, kind="action", user="ERP %s" % sup.name,
                                  text=("Commande marquée « %s » dans l'ERP du fournisseur : à vérifier%s"
                                        % (status, (" (%s)" % note) if note else ""))[:300])
        return
    if not trk and status != "completed":
        if note:
            OrderEvent.objects.create(order=order, kind="action", user="ERP %s" % sup.name, text=("Note ERP : " + note)[:300])
        return
    new = "shipped" if order.status in ("paid", "fulfilled", "production") else ""
    if not new and all(getattr(order, k, "") == v for k, v in trk.items()):
        return      # rien de neuf (l'ERP renvoie souvent la meme mise a jour)
    handle_event(sup, {"uid": order.uid, "status": new, "message": ("ERP : " + note) if note else "ERP (WooCommerce)", **trk})


def _body(request):
    try:
        return json.loads(request.body or b"{}") or {}
    except ValueError:
        from django.http import QueryDict
        return QueryDict(request.body).dict() if request.body else {}


def _err(code, msg, status, modern):
    if modern:
        return JsonResponse({"code": code, "message": msg, "data": {"status": status}}, status=status)
    return JsonResponse({"errors": [{"code": code, "message": msg}]}, status=status)


def _log(request, sup, status, note=""):
    from .models import ConnectorLog
    q = "&".join("%s=%s" % (k, "***" if k in ("consumer_secret", "oauth_signature") else v)
                 for k, v in request.GET.items())
    body = ""
    if request.method in ("POST", "PUT", "PATCH"):
        body = request.body[:4000].decode("utf-8", "replace")
    ip = (request.META.get("HTTP_X_FORWARDED_FOR") or request.META.get("REMOTE_ADDR") or "").split(",")[0].strip()
    ConnectorLog.objects.create(supplier=sup, method=request.method[:8], path=request.path[:300], query=q[:500],
                                status=status, ip=ip[:45], agent=request.META.get("HTTP_USER_AGENT", "")[:200],
                                body=body, note=note[:200])
    if sup:
        from .models import Supplier
        Supplier.objects.filter(pk=sup.pk).update(woo={**(sup.woo or {}), "last_seen": timezone.now().isoformat(timespec="seconds")})
        old = ConnectorLog.objects.filter(supplier=sup).order_by("-at").values_list("pk", flat=True)[LOG_KEEP:LOG_KEEP + 500]
        if old:
            ConnectorLog.objects.filter(pk__in=list(old)).delete()


def _index(modern):
    from .models import CompanyInfo
    name = getattr(CompanyInfo.get(), "legal_name", "") or "PaintIt"
    if modern:
        return {"name": "PaintIt", "description": name, "url": settings.SITE_URL, "home": settings.SITE_URL,
                "gmt_offset": 0, "timezone_string": "UTC",
                "namespaces": ["wc/v3", "wc/v2", "wc/v1", "wc-shipment-tracking/v3"], "authentication": {}}
    return {"store": {"name": "PaintIt", "description": name, "URL": settings.SITE_URL,
                      "wc_version": "8.0.0", "version": "3.1.0",
                      "routes": {"/orders": {"supports": ["HEAD", "GET"]},
                                 "/orders/<id>": {"supports": ["HEAD", "GET", "POST", "PUT", "PATCH"]},
                                 "/orders/<id>/notes": {"supports": ["GET", "POST"]}},
                      "meta": {"timezone": "UTC", "currency": "EUR", "currency_format": "€",
                               "currency_position": "right_space", "thousand_separator": " ",
                               "decimal_separator": ",", "price_num_decimals": 2, "tax_included": True,
                               "weight_unit": "kg", "dimension_unit": "cm", "ssl_enabled": True,
                               "permalinks_enabled": True, "generate_password": False,
                               "links": {"help": "https://woocommerce.github.io/woocommerce-rest-api-docs/"}}}}


# Transporteurs proposes a l'ERP (format du plugin WooCommerce Shipment Tracking : pays -> {nom: lien %1$s})
TRACKING_PROVIDERS = {
    "China": {"YunExpress": "https://www.yuntrack.com/parcelTracking?id=%1$s",
              "Cainiao": "https://global.cainiao.com/detail.htm?mailNoList=%1$s",
              "4PX": "https://track.4px.com/#/result/0/%1$s", "Yanwen": "https://track.yw56.com.cn/en/querydel?nums=%1$s",
              "China Post": "https://parcelsapp.com/en/tracking/%1$s"},
    "Global": {"DHL": "https://www.dhl.com/global-en/home/tracking.html?tracking-id=%1$s",
               "UPS": "https://www.ups.com/track?tracknum=%1$s", "FedEx": "https://www.fedex.com/fedextrack/?trknbr=%1$s",
               "Other": "https://parcelsapp.com/en/tracking/%1$s"},
}


def _route(request, sup, route, modern):
    """-> (payload, http_status, extra_headers, note)"""
    from .models import OrderEvent
    fmt = order_rest if modern else order_legacy
    m = request.method
    parts = [p for p in route.strip("/").split("/") if p]
    if not parts or parts[0] != "orders":
        if len(parts) == 2 and parts[0] == "products" and parts[1].isdigit():
            # produit = ligne de commande (id = commande*10 + n) : l'ERP y lit l'image ; lien produit = page des fichiers
            from .security import production_url
            pid = int(parts[1])
            order = _scope(sup).filter(pk=pid // 10).first()
            li = next((x for x in (_lines(order) if order else []) if x["id"] == pid), None)
            if not li:
                return None, 404, {}, "produit inconnu"
            imgs = [{"id": pid, "src": li["image"], "position": 0, "name": li["sku"], "alt": li["name"]}] if li["image"] else []
            if li["image"] and order:                  # l'ERP affiche les images produit : toile .tiff, tableau des couleurs, apercu
                from .security import file_url
                from .fulfillment import ensure_supplier_files
                ensure_supplier_files(order.uid)
                for k, (nm, alt) in enumerate((("%s_poster.png", "Poster: print, put in parcel / 海报：打印放入包裹"),
                                               ("%s_template.tiff", "Print file TIFF / 印刷文件"),
                                               ("%s_supplier_colors.jpg", "Colour table / 色卡"),
                                               ("%s_template.png", "Numbered canvas / 数字画布")), 1):
                    if os.path.exists(os.path.join(settings.MEDIA_ROOT, "orders", order.uid, nm % order.uid)):
                        imgs.append({"id": pid * 10 + k, "src": file_url(order.uid, nm % order.uid), "position": k,
                                     "name": nm % order.uid, "alt": alt})
            prod = {"id": pid, "title": li["name"], "name": li["name"], "sku": li["sku"], "type": "simple",
                    "status": "publish", "price": "%.2f" % li["price"], "regular_price": "%.2f" % li["price"],
                    "in_stock": True, "stock_status": "instock", "virtual": False, "weight": "",
                    "images": imgs, "featured_src": li["image"],
                    "permalink": production_url(order.uid) if order else settings.SITE_URL}
            return (prod if modern else {"product": prod}), 200, {}, "produit"
        if parts and parts[0] in ("products", "customers", "coupons", "webhooks", "reports"):
            return ([] if modern else {parts[0]: []}), 200, {}, "vide"
        if parts and parts[0] == "system_status":
            return {"environment": {"home_url": settings.SITE_URL, "version": "8.0.0"}}, 200, {}, ""
        return None, 404, {}, "route inconnue"
    if len(parts) == 2 and parts[1] == "count":
        _rows, total, _p = _filter(request, sup, modern)
        return {"count": total}, 200, {}, ""
    if len(parts) == 2 and parts[1] == "statuses":
        return {"order_statuses": WOO_STATUSES}, 200, {}, ""
    if len(parts) == 1:
        if m not in ("GET", "HEAD"):
            return None, 405, {}, "création refusée"
        rows, total, pages = _filter(request, sup, modern)
        data = [fmt(o) for o in rows]
        h = {"X-WP-Total": str(total), "X-WP-TotalPages": str(pages)} if modern else \
            {"X-WC-Total": str(total), "X-WC-TotalPages": str(pages)}
        return (data if modern else {"orders": data}), 200, h, "%d commande(s)" % len(data)
    # MangoERP appelle la commande par son id numerique OU par son numero (« 99DF2278-0A70 ») : les deux sont acceptes
    order, why = _single(sup, parts[1])
    if not order:
        return None, 404, {}, why
    data = _body(request)
    if len(parts) == 2:
        if m in ("PUT", "POST", "PATCH"):
            d = data.get("order") if isinstance(data.get("order"), dict) else data
            note = str(d.get("customer_note") or d.get("note") or "")[:300]
            _apply(sup, order, d.get("status"), _tracking_from(d, note), note)
            order.refresh_from_db()
        payload = fmt(order)
        return (payload if modern else {"order": payload}), 200, {}, ""
    if parts[2] == "notes":
        if m == "POST":
            d = data.get("order_note") if isinstance(data.get("order_note"), dict) else data
            note = str(d.get("note") or "")
            _apply(sup, order, "", _tracking_from(d, note), note[:300])
            n = {"id": int(time.time()), "created_at": _iso(timezone.now()), "note": note, "customer_note": False}
            if modern:
                n = {"id": n["id"], "author": "ERP", "date_created": _iso(timezone.now(), True), "note": note,
                     "customer_note": False}
            return (n if modern else {"order_note": n}), 201, {}, "note"
        # 1re note = remarque fournisseur (lien vers tous les fichiers) : certains ERP lisent le « 卖家备注 » ici
        notes = [{"id": 1, "created_at": _iso(order.created_at), "note": _note(order), "customer_note": True}]
        notes += [{"id": e.pk + 1, "created_at": _iso(e.at), "note": e.text, "customer_note": False}
                  for e in OrderEvent.objects.filter(order=order, kind="action").order_by("-at")[:20]]
        if modern:
            notes = [{"id": n["id"], "author": "PaintIt", "date_created": n["created_at"], "note": n["note"],
                      "customer_note": n["customer_note"]} for n in notes]
        return (notes if modern else {"order_notes": notes}), 200, {}, ""
    if parts[2] == "shipment-trackings":
        if len(parts) > 3 and parts[3] == "providers":       # liste des transporteurs (plugin Shipment Tracking)
            return TRACKING_PROVIDERS, 200, {}, "transporteurs"
        if m == "POST":
            trk = _tracking_from(data)
            _apply(sup, order, "completed", trk, "Shipment tracking")
            order.refresh_from_db()
        t = [{"tracking_id": "1", "tracking_provider": order.carrier, "tracking_link": order.tracking_url,
              "tracking_number": order.tracking_number, "date_shipped": _iso(order.status_changed_at, True)}] \
            if order.tracking_number else []
        return (t[0] if (m == "POST" and t) else t), (201 if m == "POST" else 200), {}, "tracking"
    return None, 404, {}, "route inconnue"


def _serve(request, route, modern, index_ok=True):
    if not route.strip("/") and index_ok:
        _log(request, None, 200, "index")
        return JsonResponse(_index(modern), safe=False)
    sup = authenticate(request)
    if not sup:
        _log(request, None, 401, "authentification refusée")
        code = "woocommerce_rest_cannot_view" if modern else "woocommerce_api_authentication_error"
        return _err(code, "Consumer key / secret invalide.", 401, modern)
    try:
        payload, status, headers, note = _route(request, sup, route, modern)
    except Exception as exc:     # jamais de 500 muet : l'ERP doit voir l'erreur, nous aussi
        import logging
        logging.getLogger(__name__).exception("Connecteur Woo %s", request.path)
        _log(request, sup, 500, str(exc))
        return _err("internal_error", "Erreur interne", 500, modern)
    _log(request, sup, status, note)
    if payload is None:
        return _err("woocommerce_rest_invalid" if modern else "woocommerce_api_no_route", note or "Introuvable", status, modern)
    resp = JsonResponse(payload, status=status, safe=False)
    for k, v in headers.items():
        resp[k] = v
    return resp


@csrf_exempt
def legacy(request, version, route=""):
    return _serve(request, route, modern=False)


@csrf_exempt
def wp_json(request, route=""):
    r = route.strip("/")
    if not r:
        _log(request, None, 200, "index wp-json")
        return JsonResponse(_index(True))
    for ns in ("wc/v3/", "wc/v2/", "wc/v1/", "wc-shipment-tracking/v3/", "wc-shipment-tracking/v2/"):
        if (r + "/").startswith(ns):
            return _serve(request, r[len(ns):], modern=True, index_ok=not r[len(ns):])
    _log(request, None, 404, "namespace inconnu")
    return _err("rest_no_route", "No route was found matching the URL and request method.", 404, True)
