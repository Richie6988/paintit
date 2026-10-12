"""Boutique Shopify « relais » : PaintIt reste la vitrine et encaisse (Stripe) ; chaque commande payee est
recreee dans la boutique Shopify du marchand, ou l'ERP du fournisseur (MangoERP / 芒果店长 pour Yingsa Art)
la recupere automatiquement. Quand le fournisseur expedie, son ERP pousse le suivi dans Shopify ->
webhook `fulfillments/create` (ou synchro planifiee) -> commande PaintIt « Expediee » + e-mail client.

Authentification (au choix) :
  - App du Dev Dashboard (depuis 2026) : client_id + client_secret -> jeton « client credentials »
    valable 24 h, renouvele automatiquement ;
  - ancienne app personnalisee : jeton Admin API fixe (shpat_...).
Scopes requis : write_orders, read_orders, write_products, read_products, read_fulfillments
(+ read_merchant_managed_fulfillment_orders pour la synchro des suivis).
"""
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)
API_VERSION = "2025-07"
PRODUCT_TITLE = "PaintIt - Peinture par numéros personnalisée"
BRUSH_SKU = "PBN-BRUSHES"
TAG = "paintit"


class ShopifyError(Exception):
    pass


def cfg(sup):
    return dict(sup.shopify or {})


def _save(sup, **kw):
    from .models import Supplier
    c = cfg(sup); c.update(kw)
    sup.shopify = c
    Supplier.objects.filter(pk=sup.pk).update(shopify=c)


def domain(sup):
    d = (cfg(sup).get("domain") or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").split("/")[0]
    if d and "." not in d:
        d += ".myshopify.com"
    return d


def configured(sup):
    c = cfg(sup)
    return bool(domain(sup) and (c.get("static_token") or (c.get("client_id") and c.get("client_secret"))))


def _http(url, data=None, headers=None, form=False, timeout=25):
    body = None
    h = dict(headers or {})
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    h.setdefault("User-Agent", "PaintIt-Shopify/1")
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload, dict(e.headers or {})


def token(sup, force=False):
    """Jeton Admin API : fixe, ou client credentials (24 h) mis en cache et renouvele 1 h avant expiration."""
    c = cfg(sup)
    if c.get("static_token"):
        return c["static_token"]
    if not force and c.get("token") and float(c.get("token_exp") or 0) > time.time() + 3600:
        return c["token"]
    if not (c.get("client_id") and c.get("client_secret") and domain(sup)):
        raise ShopifyError("Shopify non configuré : domaine + Client ID/Secret (ou jeton Admin API) requis.")
    st, data, _h = _http("https://%s/admin/oauth/access_token" % domain(sup), {
        "grant_type": "client_credentials", "client_id": c["client_id"], "client_secret": c["client_secret"]}, form=True)
    if st != 200 or not data.get("access_token"):
        raise ShopifyError("Jeton refusé par Shopify (HTTP %s) : %s" % (st, data.get("error_description") or data.get("error")
                                                                      or data or "réponse vide"))
    _save(sup, token=data["access_token"], token_exp=time.time() + int(data.get("expires_in") or 86399))
    return data["access_token"]


def gql(sup, query, variables=None, _retry=True):
    url = "https://%s/admin/api/%s/graphql.json" % (domain(sup), cfg(sup).get("api_version") or API_VERSION)
    st, data, _h = _http(url, {"query": query, "variables": variables or {}},
                         headers={"X-Shopify-Access-Token": token(sup)})
    if st == 401 and _retry and not cfg(sup).get("static_token"):
        token(sup, force=True)
        return gql(sup, query, variables, _retry=False)
    if st == 429 and _retry:
        time.sleep(2)
        return gql(sup, query, variables, _retry=False)
    if st != 200:
        raise ShopifyError("Shopify HTTP %s : %s" % (st, data.get("errors") or data))
    if data.get("errors"):
        throttled = any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in data["errors"])
        if throttled and _retry:
            time.sleep(2)
            return gql(sup, query, variables, _retry=False)
        raise ShopifyError("Shopify : %s" % "; ".join(e.get("message", str(e)) for e in data["errors"]))
    return data.get("data") or {}


def _user_errors(payload):
    errs = (payload or {}).get("userErrors") or []
    return "; ".join("%s%s" % (e.get("message"), (" (%s)" % ".".join(map(str, e["field"]))) if e.get("field") else "")
                     for e in errs)


# ---------------------------------------------------------------- connexion
def test(sup):
    d = gql(sup, "{ shop { name myshopifyDomain currencyCode } }")
    shop = d.get("shop") or {}
    _save(sup, shop_name=shop.get("name"), currency=shop.get("currencyCode"),
          last_check=timezone.now().isoformat(timespec="seconds"))
    return shop


# ---------------------------------------------------------------- catalogue
def sku_for(fmt_key, colors):
    return "PBN-%s-%dC" % (fmt_key, int(colors))


def catalogue(sup=None):
    """Variantes a publier : formats du Kit x nombres de couleurs disponibles (onglet Tarifs)."""
    from .models import KitFormat, Pricing
    p = Pricing.get()
    out = []
    for kf in KitFormat.objects.filter(available=True):
        for n in p.available_colors():
            out.append({"format": kf.key, "colors": n, "sku": sku_for(kf.key, n),
                        "price": round(float(kf.price) + p.color_price(n), 2)})
    return out


def sync_catalogue(sup):
    """Cree / met a jour le produit Shopify (une variante par format x couleurs, SKU PBN-40x50-24C)
    + le produit « Set de pinceaux ». Le fournisseur associe ces SKU a ses produits dans son ERP."""
    items = catalogue(sup)
    if not items:
        raise ShopifyError("Aucun format/couleur disponible dans l'onglet Tarifs.")
    fmts = list(dict.fromkeys(i["format"] for i in items))
    cols = list(dict.fromkeys(str(i["colors"]) for i in items))
    c = cfg(sup)

    def variants(sku_inside):
        vs = []
        for i in items:
            v = {"optionValues": [{"optionName": "Format", "name": i["format"]},
                                  {"optionName": "Couleurs", "name": str(i["colors"])}],
                 "price": "%.2f" % i["price"]}
            if sku_inside:
                v["inventoryItem"] = {"sku": i["sku"], "tracked": False, "requiresShipping": True}
            else:
                v["sku"] = i["sku"]
            vs.append(v)
        return vs

    q = """mutation($input: ProductSetInput!) { productSet(input: $input, synchronous: true) {
             product { id variants(first: 250) { nodes { id sku } } }
             userErrors { field message } } }"""
    base = {"title": PRODUCT_TITLE, "vendor": "PaintIt", "productType": "Paint by numbers", "status": "ACTIVE",
            "tags": [TAG], "productOptions": [{"name": "Format", "values": [{"name": f} for f in fmts]},
                                               {"name": "Couleurs", "values": [{"name": x} for x in cols]}]}
    if c.get("product_id"):
        base["id"] = c["product_id"]
    res, err = None, ""
    for sku_inside in (True, False):   # selon la version d'API, le SKU est dans inventoryItem ou a la racine
        try:
            res = gql(sup, q, {"input": dict(base, variants=variants(sku_inside))}).get("productSet") or {}
        except ShopifyError as e:
            err = str(e); res = None
            continue
        err = _user_errors(res)
        if not err:
            break
    if not res or err or not res.get("product"):
        raise ShopifyError("Création du produit impossible : %s" % (err or "réponse vide"))
    prod = res["product"]
    vmap = {n["sku"]: n["id"] for n in prod["variants"]["nodes"] if n.get("sku")}
    # Set de pinceaux (produit separe, SKU fixe)
    bq = {"title": "PaintIt - Set de pinceaux", "vendor": "PaintIt", "status": "ACTIVE", "tags": [TAG],
          "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}]}
    if c.get("brush_product_id"):
        bq["id"] = c["brush_product_id"]
    from .models import Pricing
    bprice = "%.2f" % Pricing.get().brushes_price
    brush_id = c.get("brush_product_id")
    for sku_inside in (True, False):
        v = {"optionValues": [{"optionName": "Title", "name": "Default Title"}], "price": bprice}
        v.update({"inventoryItem": {"sku": BRUSH_SKU, "tracked": False, "requiresShipping": True}} if sku_inside
                 else {"sku": BRUSH_SKU})
        try:
            r2 = gql(sup, q, {"input": dict(bq, variants=[v])}).get("productSet") or {}
        except ShopifyError:
            continue
        if not _user_errors(r2) and r2.get("product"):
            brush_id = r2["product"]["id"]
            for n in r2["product"]["variants"]["nodes"]:
                vmap[n.get("sku") or BRUSH_SKU] = n["id"]
            break
    _save(sup, product_id=prod["id"], brush_product_id=brush_id, variants=vmap,
          catalogue_synced=timezone.now().isoformat(timespec="seconds"))
    return len(vmap)


# ---------------------------------------------------------------- commandes
def _split_name(full):
    parts = (full or "").strip().split()
    if len(parts) <= 1:
        return (parts[0] if parts else "Client"), "."
    return " ".join(parts[:-1]), parts[-1]


def _phone(code, number):
    n = "".join(ch for ch in (number or "") if ch.isdigit())
    c = "".join(ch for ch in (code or "") if ch.isdigit() or ch == "+")
    if not n:
        return None
    if c and not n.startswith(c.lstrip("+")):
        n = n.lstrip("0")
        return "+%s%s" % (c.lstrip("+"), n)
    return "+" + n


def variant_title(order):
    """Titre de variante tel que Shopify l'affiche (options Format / Couleurs) : « 40x50 / 24 »."""
    from .models import format_key
    return "%s / %d" % (format_key(order.width_cm, order.height_cm), int(order.colors))


def line_properties(order):
    """Proprietes de ligne de commande (vues par l'ERP du fournisseur) : identiques dans Shopify et dans l'Excel
    MangoERP. Fichiers = ce dont le fournisseur a besoin : poster, toile .tiff, JSON des couleurs (+ ZIP des 3)."""
    from .security import file_url
    u = order.uid
    return [("Commande PaintIt", u),
            ("Format", "%s (%s)" % (order.format_label, order.orientation)),
            ("Couleurs", str(order.colors)),
            ("Poster", file_url(u, "%s_poster.png" % u)),
            ("Toile numérotée (TIFF)", file_url(u, "%s_template.tiff" % u)),
            ("Fiche couleurs (JSON)", file_url(u, "%s_supplier.json" % u)),
            ("Fichiers (ZIP)", file_url(u, "production.zip"))]


def push_order(sup, order, files):
    """Cree la commande dans Shopify (payee, sans e-mail Shopify au client). -> nom Shopify (#1001)."""
    from . import address
    if order.shopify_order_id:
        return "deja creee (%s)" % order.shopify_order_id
    c = cfg(sup)
    from .models import format_key
    fmt = format_key(order.width_cm, order.height_cm)
    sku = sku_for(fmt, order.colors)
    variant = (c.get("variants") or {}).get(sku)
    cur = c.get("currency") or "EUR"
    # remise eventuelle repartie au prorata sur les lignes : total Shopify = montant reellement encaisse
    price = float(order.price or 0)
    total = float(order.total or price)
    k = (total / price) if price else 1.0
    canvas = round((price - float(order.brushes_amount or 0)) * k, 2)
    brush_amt = round(total - canvas, 2) if order.brushes else 0.0
    props = [{"name": n, "value": v} for n, v in line_properties(order)]
    li = {"quantity": 1, "properties": props,
          "priceSet": {"shopMoney": {"amount": "%.2f" % canvas, "currencyCode": cur}}}
    if variant:
        li["variantId"] = variant
    else:   # catalogue pas encore synchronise : ligne personnalisee avec le SKU
        li.update({"title": "PaintIt %s · %d couleurs" % (order.format_label, order.colors), "sku": sku,
                   "requiresShipping": True})
    lines = [li]
    if order.brushes:
        b = {"quantity": 1, "priceSet": {"shopMoney": {"amount": "%.2f" % brush_amt, "currencyCode": cur}}}
        if (c.get("variants") or {}).get(BRUSH_SKU):
            b["variantId"] = c["variants"][BRUSH_SKU]
        else:
            b.update({"title": "Set de pinceaux", "sku": BRUSH_SKU, "requiresShipping": True})
        lines.append(b)
    first, last = _split_name(order.customer_name)
    iso = (address.COUNTRIES.get(order.country) or ("FR",))[0]
    phone = _phone(order.phone_code, order.phone)
    ship = {"firstName": first, "lastName": last, "address1": order.address1 or "", "address2": order.address2 or "",
            "city": order.city or "", "zip": order.postal_code or "", "countryCode": iso}
    if phone:
        ship["phone"] = phone
    inp = {"lineItems": lines, "email": order.customer_email, "shippingAddress": ship, "billingAddress": ship,
           "financialStatus": "PAID", "currency": cur, "tags": [TAG, "paintit-" + order.uid],
           "note": "Commande PaintIt %s · fichiers de production dans les propriétés de la ligne." % order.uid,
           "customAttributes": [{"key": "paintit_uid", "value": order.uid}],
           "transactions": [{"kind": "SALE", "status": "SUCCESS", "gateway": "stripe (paintit.click)",
                             "amountSet": {"shopMoney": {"amount": "%.2f" % total, "currencyCode": cur}}}]}
    q = """mutation($order: OrderCreateOrderInput!, $options: OrderCreateOptionsInput) {
             orderCreate(order: $order, options: $options) { order { id name legacyResourceId } userErrors { field message } } }"""
    opts = {"inventoryBehaviour": "BYPASS", "sendReceipt": False, "sendFulfillmentReceipt": False}
    res = gql(sup, q, {"order": inp, "options": opts}).get("orderCreate") or {}
    err = _user_errors(res)
    if err or not res.get("order"):
        raise ShopifyError("orderCreate : %s" % (err or "réponse vide"))
    o = res["order"]
    from .models import Order
    Order.objects.filter(pk=order.pk).update(shopify_order_id=str(o.get("legacyResourceId") or o["id"]),
                                             supplier_ref=(o.get("name") or "")[:64])
    return o.get("name") or o["id"]


# ---------------------------------------------------------------- retours (suivi)
def verify_webhook(sup, raw_body, hmac_header):
    secret = (cfg(sup).get("client_secret") or cfg(sup).get("webhook_secret") or "").encode()
    if not secret or not hmac_header:
        return False
    digest = base64.b64encode(hmac.new(secret, raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, hmac_header.strip())


def webhook_url(sup):
    return "%s/api/shopify/%s/webhook/" % (settings.SITE_URL, sup.webhook_secret)


def install_webhooks(sup):
    """Abonne la boutique aux expeditions / annulations -> notre URL. -> liste des sujets actifs."""
    url = webhook_url(sup)
    existing = gql(sup, "{ webhookSubscriptions(first: 50) { nodes { id topic endpoint { __typename "
                        "... on WebhookHttpEndpoint { callbackUrl } } } } }")
    have = {n["topic"] for n in (existing.get("webhookSubscriptions") or {}).get("nodes", [])
            if ((n.get("endpoint") or {}).get("callbackUrl") or "") == url}
    done = []
    for topic in ("FULFILLMENTS_CREATE", "FULFILLMENTS_UPDATE", "ORDERS_CANCELLED"):
        if topic in have:
            done.append(topic); continue
        q = """mutation($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
                 webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
                   webhookSubscription { id } userErrors { field message } } }"""
        err = ""
        for key in ("callbackUrl", "uri"):
            try:
                r = gql(sup, q, {"topic": topic, "sub": {key: url, "format": "JSON"}}).get("webhookSubscriptionCreate") or {}
            except ShopifyError as e:
                err = str(e); continue
            err = _user_errors(r)
            if not err:
                done.append(topic); break
        if topic not in done:
            raise ShopifyError("Webhook %s : %s" % (topic, err))
    _save(sup, webhooks=done)
    return done


def _apply_tracking(sup, order, company, number, url, status="shipped", note=""):
    from .integrations import handle_event
    return handle_event(sup, {"uid": order.uid, "status": status, "carrier": company or "",
                              "tracking_number": number or "", "tracking_url": url or "",
                              "message": note or "Shopify"})


def handle_webhook(sup, topic, data):
    from .models import Order
    topic = (topic or "").lower()
    if topic.startswith("fulfillments/"):
        order = Order.objects.filter(shopify_order_id=str(data.get("order_id") or "")).first()
        if not order:
            return 200, "commande inconnue (ignorée)"
        if (data.get("status") or "") in ("cancelled", "error", "failure"):
            return 200, "expédition annulée (ignorée)"
        nums = data.get("tracking_numbers") or [data.get("tracking_number")]
        urls = data.get("tracking_urls") or [data.get("tracking_url")]
        delivered = (data.get("shipment_status") or "") == "delivered"
        return _apply_tracking(sup, order, data.get("tracking_company"), (nums or [""])[0], (urls or [""])[0],
                               "delivered" if delivered else "shipped", "Shopify : expédition")
    if topic == "orders/cancelled":
        order = Order.objects.filter(shopify_order_id=str(data.get("id") or "")).first()
        if order:
            from .models import OrderEvent
            OrderEvent.objects.create(order=order, kind="action", user="Shopify %s" % sup.name,
                                      text="Commande annulée côté Shopify / fournisseur : à vérifier (remboursement ?)")
        return 200, "ok"
    return 200, "sujet ignoré"


def poll_tracking(sup, days=45):
    """Filet de securite (cron) : relit les expeditions Shopify des commandes PaintIt encore ouvertes."""
    from .models import Order
    open_orders = {o.shopify_order_id: o for o in Order.objects.filter(
        supplier=sup, status__in=(Order.PAID, Order.FULFILLED, Order.SHIPPED)).exclude(shopify_order_id="")}
    if not open_orders:
        return 0
    n = 0
    ids = list(open_orders)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        q = "{ nodes(ids: [%s]) { ... on Order { legacyResourceId displayFulfillmentStatus cancelledAt " \
            "fulfillments(first: 5) { status displayStatus trackingInfo(first: 1) { company number url } } } } }" % \
            ",".join('"gid://shopify/Order/%s"' % x for x in chunk)
        for node in (gql(sup, q).get("nodes") or []):
            if not node:
                continue
            order = open_orders.get(str(node.get("legacyResourceId")))
            if not order:
                continue
            for f in node.get("fulfillments") or []:
                ti = (f.get("trackingInfo") or [{}])
                ti = ti[0] if ti else {}
                if f.get("status") in ("SUCCESS", "OPEN") and (ti.get("number") or f.get("displayStatus")):
                    delivered = (f.get("displayStatus") or "") == "DELIVERED"
                    if (order.tracking_number != (ti.get("number") or order.tracking_number)) or \
                            (delivered and order.status != Order.DELIVERED) or order.status in (Order.PAID, Order.FULFILLED):
                        _apply_tracking(sup, order, ti.get("company"), ti.get("number"), ti.get("url"),
                                        "delivered" if delivered else "shipped", "Shopify : synchro")
                        n += 1
                    break
    return n
