"""Boutique WooCommerce de TEST : a brancher dans MangoERP comme une boutique de plus, pour reperer ou
chaque information arrive et en faire un tuto pour le fournisseur.

- fournisseur INACTIF (ne recoit jamais de vraie commande), mais lisible par l'ERP (woo["test"]) ;
- commandes TEST… : copie des fichiers d'une commande existante (jamais la photo d'origine ni le bon interne),
  destinataire « TEST - NE PAS EXPEDIER », is_test (hors compta / stats).
"""
import json
import os
import shutil
import uuid

from django.conf import settings

SHOP_NAME = "Boutique test ERP (temporaire)"
TEST_NAME = "TEST - NE PAS EXPEDIER / 测试 请勿发货"


def get_shop():
    from .models import Supplier
    return Supplier.objects.filter(name=SHOP_NAME).first()


def create_shop():
    from .models import Supplier
    from . import woo_api
    shop = get_shop() or Supplier.objects.create(
        name=SHOP_NAME, active=False, integration="woo", checkout="kit", auto_dispatch=False,
        notes="Démo ERP fournisseur : à supprimer une fois le tuto fait.")
    if not (shop.woo or {}).get("ck"):
        woo_api.generate_keys(shop)
    if not shop.woo.get("test"):
        shop.woo = {**shop.woo, "test": True}           # lisible par l'ERP bien qu'inactive
        shop.save(update_fields=["woo"])
    return shop


def _source(uid=""):
    from .models import Order
    qs = Order.objects.exclude(uid__startswith="TEST").order_by("-created_at")
    if uid:
        qs = qs.filter(uid=uid)
    for o in qs[:50]:
        if os.path.isfile(os.path.join(settings.MEDIA_ROOT, "orders", o.uid, "%s_poster.png" % o.uid)):
            return o
    raise ValueError("aucune commande avec fichiers de production (poster) à copier")


def _copy_files(old, new):
    sd, dd = (os.path.join(settings.MEDIA_ROOT, "orders", u) for u in (old, new))
    os.makedirs(dd, exist_ok=True)
    for n in os.listdir(sd):
        if not n.startswith(old) or "_source" in n or n.endswith("_order.json"):
            continue          # jamais la photo d'origine ni le bon de commande interne (prix)
        p = os.path.join(sd, n)
        if not os.path.isfile(p):
            continue
        out = os.path.join(dd, n.replace(old, new, 1))
        if n.endswith((".json", ".txt", ".svg")):
            with open(p, encoding="utf-8", errors="replace") as f:
                txt = f.read().replace(old, new)
            with open(out, "w", encoding="utf-8") as f:
                f.write(txt)
        else:
            shutil.copyfile(p, out)


def _anonymise(o):
    """Fiche fournisseur de la copie : destinataire TEST, puis TXT + tableau couleurs regeneres."""
    from .fulfillment import write_supplier_text, ensure_supplier_files
    p = os.path.join(settings.MEDIA_ROOT, "orders", o.uid, "%s_supplier.json" % o.uid)
    try:
        with open(p, encoding="utf-8") as f:
            sj = json.load(f)
    except (OSError, ValueError):
        sj = None
    if sj is not None:
        sj.setdefault("consignee_information", {}).update({"full_name": o.customer_name, "email": o.customer_email})
        with open(p, "w", encoding="utf-8") as f:
            json.dump(sj, f, ensure_ascii=False, indent=2)
        write_supplier_text(o.uid, sj)
    ensure_supplier_files(o.uid)


def add_order(shop, src_uid="", user=""):
    """Nouvelle commande TEST visible par l'ERP de la boutique test. -> Order"""
    from .models import Order
    from . import integrations
    src = _source(src_uid)
    uid = "TEST%s-%s" % (uuid.uuid4().hex[:4].upper(), uuid.uuid4().hex[:4].upper())
    _copy_files(src.uid, uid)
    o = Order.objects.create(
        uid=uid, status=Order.FULFILLED, is_test=True, format_label=src.format_label, orientation=src.orientation,
        width_cm=src.width_cm, height_cm=src.height_cm, colors=src.colors, brushes=src.brushes, price=0, total=0,
        customer_name=TEST_NAME, customer_email="test@paintit.click",
        phone_code=src.phone_code, phone=src.phone, address1=src.address1, address2=src.address2,
        postal_code=src.postal_code, city=src.city, country=src.country,
        notes="Commande de démonstration ERP (copie de %s)" % src.uid)
    if not integrations.dispatch({"uid": uid}, {}, sup=shop, user=user or "boutique test"):
        raise ValueError("transmission à la boutique test impossible")
    _anonymise(o)
    return o


def test_orders(shop):
    from .models import Order
    return Order.objects.filter(supplier=shop, is_test=True, uid__startswith="TEST").order_by("-created_at")


def delete_shop(shop):
    for o in test_orders(shop):
        shutil.rmtree(os.path.join(settings.MEDIA_ROOT, "orders", o.uid), ignore_errors=True)
        o.delete()
    shop.delete()


def field_map(o):
    """Chaque information envoyee a l'ERP : (cle, information, champ WooCommerce, valeur)."""
    from .woo_api import order_legacy, _lines
    from .security import production_url
    lg = order_legacy(o)
    ship, li = lg["shipping_address"], lg["line_items"][0]
    rows = [
        ("number", "N° de commande", "order_number / number", lg["order_number"]),
        ("name", "Destinataire", "shipping.first_name + last_name", "%s %s" % (ship["first_name"], ship["last_name"])),
        ("phone", "Téléphone", "billing.phone", lg["billing_address"]["phone"]),
        ("address", "Adresse", "shipping.address_1 / address_2", " ".join(x for x in (ship["address_1"], ship["address_2"]) if x)),
        ("city", "Ville / CP / pays", "shipping.city / postcode / country", "%s %s %s" % (ship["city"], ship["postcode"], ship["country"])),
        ("sku", "SKU (à associer : 配对)", "line_items[].sku", ", ".join(x["sku"] for x in lg["line_items"])),
        ("product", "Nom du produit", "line_items[].name", li["name"]),
        ("price", "Prix d'achat", "line_items[].price / total", "%s EUR" % lg["total"]),
        ("image", "Image de la ligne (poster)", "line_items[].image.src", li["image"]["src"]),
        ("note", "Remarque : lien « All files / 全部文件 »", "customer_note / note", lg["note"]),
        ("order_note", "Note de commande (même texte)", "GET /orders/<id>/notes", lg["note"]),
        ("files_page", "Page de tous les fichiers (ZIP)", "dans la remarque", production_url(o.uid)),
    ]
    rows += [("meta_%d" % i, "Propriété de ligne : %s" % m["key"], "line_items[].meta / meta_data", m["value"])
             for i, m in enumerate(li["meta"])]
    pid = _lines(o)[0]["id"]
    rows.append(("product_images", "Images produit (poster, TIFF, couleurs, toile)", "GET /products/%d → images[]" % pid,
                 "4 images"))
    return rows
