"""Rapprochement comptable quotidien (cron, ex. 3h) :
    15 3 * * * cd /home/paintit/Django && .venv/bin/python manage.py ledger_sync
- enregistre les commandes payees absentes du grand livre ;
- importe de Stripe les ventes manquantes, frais reels, remboursements et virements vers la banque.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Rapproche le grand livre avec les commandes et le solde Stripe."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=40)

    def handle(self, *args, **opts):
        from studio import ledger
        n = ledger.sync_orders()
        r = ledger.sync_stripe(days=opts["days"])
        self.stdout.write(self.style.SUCCESS("Commandes rattrapées : %d · Stripe : %s" % (n, r)))
