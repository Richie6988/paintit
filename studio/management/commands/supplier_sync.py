"""Relance automatique des transmissions fournisseur (a planifier en cron, ex. toutes les 15 min) :
    */15 * * * * cd /home/paintit/Django && venv/bin/python manage.py supplier_sync
- retransmet les commandes dont la derniere transmission a echoue (max 5 essais) ;
- transmet les commandes payees jamais transmises (fournisseur en transmission automatique).
"""
import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

from studio import integrations
from studio.models import Order, OrderEvent, Supplier


class Command(BaseCommand):
    help = "Relance les transmissions fournisseur en echec et envoie les commandes payees non transmises."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        from studio.views import _order_from_row, _shipping_from_row
        dry = opts["dry_run"]
        todo = {o.pk: o for o in integrations.pending_retries()}
        # payees depuis > 10 min, jamais transmises, fournisseur kit en mode automatique
        kit = Supplier.for_checkout("kit")
        if kit and kit.auto_dispatch:
            sent = OrderEvent.objects.filter(kind="action", text__startswith=integrations.OK_PREFIX).values("order_id")
            for o in Order.objects.filter(status__in=(Order.PAID, Order.FULFILLED), supplier__isnull=True,
                                          created_at__lt=timezone.now() - datetime.timedelta(minutes=10),
                                          created_at__gte=timezone.now() - datetime.timedelta(days=30)) \
                    .exclude(pk__in=sent):
                todo.setdefault(o.pk, o)
        ok = ko = 0
        for o in todo.values():
            self.stdout.write("%s -> %s" % (o.uid, (o.supplier or kit).name if (o.supplier or kit) else "?"))
            if dry:
                continue
            if integrations.dispatch(_order_from_row(o), _shipping_from_row(o), sup=o.supplier, user="relance auto"):
                ok += 1
                if o.status == Order.PAID:
                    Order.objects.filter(pk=o.pk).update(status=Order.FULFILLED)
            else:
                ko += 1
        self.stdout.write(self.style.SUCCESS("%d transmise(s), %d echec(s), %d a traiter." % (ok, ko, len(todo))))
        # Demande d'avis (verifie) : aussi pour les commandes expediees depuis assez longtemps sans statut « livree »
        from studio import emails
        from studio.models import Pricing
        wait = int(Pricing.get().delivery_days_max or 9) + 2
        for o in Order.objects.filter(status__in=(Order.SHIPPED, Order.DELIVERED), feedback_sent=False,
                                      status_changed_at__lt=timezone.now() - datetime.timedelta(days=wait))[:50]:
            if dry:
                continue
            try:
                emails.send_feedback_request(o)
                Order.objects.filter(pk=o.pk).update(feedback_sent=True)
            except Exception as exc:
                self.stdout.write(self.style.ERROR("Avis %s : %s" % (o.uid, exc)))
        # Boutiques Shopify relais : relit les expeditions (au cas ou un webhook serait perdu)
        from studio import shopify
        for sup in Supplier.objects.filter(active=True, integration="shopify"):
            if dry or not shopify.configured(sup):
                continue
            try:
                n = shopify.poll_tracking(sup)
                self.stdout.write("Shopify %s : %d suivi(s) mis a jour" % (sup.name, n))
            except Exception as exc:
                self.stdout.write(self.style.ERROR("Shopify %s : %s" % (sup.name, exc)))
