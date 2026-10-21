"""Boutique WooCommerce de TEST (aussi disponible dans l'ERP : Fournisseurs → « Créer une boutique test »).

    python manage.py woo_test_shop                 # cree la boutique + 1 commande TEST
    python manage.py woo_test_shop --from 81288CD0-A351
    python manage.py woo_test_shop --delete        # supprime boutique, commandes TEST et fichiers
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Cree / supprime une boutique WooCommerce de test avec une commande TEST pour l'ERP du fournisseur."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="src", default="", help="uid de la commande a copier (defaut : la plus recente avec fichiers)")
        parser.add_argument("--delete", action="store_true")

    def handle(self, *args, **opts):
        from studio import woo_test
        from studio.security import production_url
        if opts["delete"]:
            shop = woo_test.get_shop()
            if shop:
                woo_test.delete_shop(shop)
            self.stdout.write(self.style.SUCCESS("Boutique test supprimee."))
            return
        shop = woo_test.create_shop()
        try:
            o = woo_test.add_order(shop, opts["src"], user="woo_test_shop")
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS("Boutique test prete. A saisir dans l'ERP (nouvelle boutique WooCommerce) :"))
        self.stdout.write("  URL boutique    : %s" % settings.SITE_URL)
        self.stdout.write("  Consumer key    : %s" % shop.woo["ck"])
        self.stdout.write("  Consumer secret : %s" % shop.woo["cs"])
        self.stdout.write("  Commande TEST   : %s" % o.uid)
        self.stdout.write("  Page fichiers   : %s" % production_url(o.uid))
        self.stdout.write("Suppression ensuite : python manage.py woo_test_shop --delete")
