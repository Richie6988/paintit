"""Complete le dossier fournisseur des commandes payees (TIFF HD, poster, order.json).
Utile pour les commandes passees avant le correctif (generation interrompue apres paiement).
    python manage.py ensure_order_files            # toutes les commandes payees
    python manage.py ensure_order_files UID ...    # commandes precises
    python manage.py ensure_order_files --dry-run
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand

from studio.models import Order


class Command(BaseCommand):
    help = "Genere les fichiers fournisseur manquants (TIFF, poster, order.json) des commandes payees."

    def add_arguments(self, parser):
        parser.add_argument("uids", nargs="*")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from studio.views import (ensure_supplier_files, supplier_files_status,
                                  _order_from_row, _shipping_from_row)
        qs = Order.objects.filter(status__in=Order.PAID_STATUSES)
        if opts["uids"]:
            qs = Order.objects.filter(uid__in=opts["uids"])
        done = incomplete = skipped = 0
        for o in qs.order_by("created_at"):
            d = os.path.join(settings.MEDIA_ROOT, "orders", o.uid)
            if not os.path.isdir(d) or not os.path.exists(os.path.join(d, "%s_template.svg" % o.uid)):
                skipped += 1
                self.stdout.write(self.style.WARNING("%s : toile absente du serveur, ignoree" % o.uid))
                continue
            missing = [f["label"] for f in supplier_files_status(o.uid) if not f["ok"]]
            if not missing:
                continue
            self.stdout.write("%s : manque %s" % (o.uid, ", ".join(missing)))
            if opts["dry_run"]:
                continue
            left = ensure_supplier_files(_order_from_row(o), _shipping_from_row(o), user="rattrapage")
            if left:
                incomplete += 1
                self.stdout.write(self.style.ERROR("   toujours manquant : %s" % ", ".join(left)))
            else:
                done += 1
        self.stdout.write(self.style.SUCCESS("%d commande(s) completee(s), %d incomplete(s), %d ignoree(s)."
                                             % (done, incomplete, skipped)))
