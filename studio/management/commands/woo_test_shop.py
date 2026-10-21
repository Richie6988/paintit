"""Boutique WooCommerce de TEST (temporaire) pour montrer au fournisseur ou trouver les donnees dans son ERP.

    python manage.py woo_test_shop                 # cree la boutique + 1 commande TEST (copie d'une commande existante)
    python manage.py woo_test_shop --from 81288CD0-A351
    python manage.py woo_test_shop --delete        # supprime boutique, commande(s) TEST et fichiers

La boutique est un fournisseur INACTIF : elle ne recoit jamais de vraie commande. La commande est marquee
is_test (exclue de la compta / des stats) et le destinataire dit « TEST - NE PAS EXPEDIER / 测试 请勿发货 ».
"""
import os
import shutil
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

SHOP_NAME = "Boutique test ERP (temporaire)"


class Command(BaseCommand):
    help = "Cree / supprime une boutique WooCommerce de test avec une commande TEST pour l'ERP du fournisseur."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="src", default="", help="uid de la commande a copier (defaut : la plus recente avec fichiers)")
        parser.add_argument("--delete", action="store_true")

    def handle(self, *args, **opts):
        from studio.models import Order, Supplier
        shop = Supplier.objects.filter(name=SHOP_NAME).first()
        if opts["delete"]:
            for o in Order.objects.filter(supplier=shop, is_test=True) if shop else []:
                shutil.rmtree(os.path.join(settings.MEDIA_ROOT, "orders", o.uid), ignore_errors=True)
                o.delete()
            if shop:
                shop.delete()
            self.stdout.write(self.style.SUCCESS("Boutique test supprimee."))
            return
        src = self._source(opts["src"])
        from studio import woo_api, integrations
        if not shop:
            shop = Supplier.objects.create(name=SHOP_NAME, active=False, integration="woo", checkout="kit",
                                           auto_dispatch=False, notes="Demo ERP fournisseur - supprimer avec "
                                                                      "manage.py woo_test_shop --delete")
        keys = shop.woo if (shop.woo or {}).get("ck") else woo_api.generate_keys(shop)
        if not keys.get("test"):
            shop.woo = keys = {**keys, "test": True}         # lisible par l'ERP bien qu'inactive
            shop.save(update_fields=["woo"])
        uid = "TEST%s-%s" % (uuid.uuid4().hex[:4].upper(), uuid.uuid4().hex[:4].upper())
        self._copy_files(src.uid, uid)
        o = Order.objects.create(
            uid=uid, status=Order.FULFILLED, is_test=True, format_label=src.format_label, orientation=src.orientation,
            width_cm=src.width_cm, height_cm=src.height_cm, colors=src.colors, brushes=src.brushes, price=0, total=0,
            customer_name="TEST - NE PAS EXPEDIER / 测试 请勿发货", customer_email="test@paintit.click",
            phone_code=src.phone_code, phone=src.phone, address1=src.address1, address2=src.address2,
            postal_code=src.postal_code, city=src.city, country=src.country, notes="Commande de demonstration ERP")
        if not integrations.dispatch({"uid": uid}, {}, sup=shop, user="woo_test_shop"):
            raise CommandError("transmission a la boutique test impossible")
        self._anonymise(o)
        self.stdout.write(self.style.SUCCESS("Boutique test prete. A saisir dans l'ERP (nouvelle boutique WooCommerce) :"))
        self.stdout.write("  URL boutique    : %s" % settings.SITE_URL)
        self.stdout.write("  Consumer key    : %s" % keys["ck"])
        self.stdout.write("  Consumer secret : %s" % keys["cs"])
        self.stdout.write("  Commande TEST   : %s (copie de %s)" % (uid, src.uid))
        from studio.security import production_url
        self.stdout.write("  Page fichiers   : %s" % production_url(uid))
        self.stdout.write("Suppression ensuite : python manage.py woo_test_shop --delete")

    def _anonymise(self, o):
        """Fiche fournisseur de la copie : destinataire TEST, puis TXT + tableau couleurs regeneres."""
        import json
        from studio.fulfillment import write_supplier_text, ensure_supplier_files
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

    def _source(self, uid):
        from studio.models import Order
        qs = Order.objects.exclude(uid__startswith="TEST").order_by("-created_at")
        if uid:
            qs = qs.filter(uid=uid)
        for o in qs[:50]:
            if os.path.isfile(os.path.join(settings.MEDIA_ROOT, "orders", o.uid, "%s_poster.png" % o.uid)):
                return o
        raise CommandError("aucune commande avec fichiers de production (poster) a copier")

    def _copy_files(self, old, new):
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
