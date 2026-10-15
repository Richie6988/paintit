"""Purge RGPD des donnees INUTILES : apercus jamais commandes, commandes jamais payees, uploads bruts.

Ne touche JAMAIS : les commandes payees elles-memes (pieces comptables, 10 ans), les modeles de galerie
(gal-/lib-/mkt-), ni les toiles de "Mes toiles" (DigitalCanvas).
Fichiers de fabrication d'une commande payee (photo, toile, poster) : supprimes N jours apres l'expedition
(--production-days, 60 par defaut), sauf si la toile est toujours dans "Mes toiles".

A planifier en cron quotidien, par ex. :
    python manage.py purge_old_data            # > 30 jours
    python manage.py purge_old_data --days 30 --dry-run
"""
import os
import shutil
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from studio.models import DigitalCanvas, Order, OrderEvent

PROTECTED_PREFIXES = ("gal-", "lib-", "mkt-")
PURGED_TEXT = "Fichiers de fabrication supprimés (conservation %d j après expédition)"


def production_expired(days=60):
    """Commandes expediees / livrees depuis plus de `days` jours dont le dossier existe encore
    et dont la toile n'est plus dans « Mes toiles »."""
    cutoff = timezone.now() - timedelta(days=days)
    mine = set(DigitalCanvas.objects.values_list("uid", flat=True))
    root = os.path.join(settings.MEDIA_ROOT, "orders")
    for o in Order.objects.filter(status__in=(Order.SHIPPED, Order.DELIVERED), status_changed_at__lt=cutoff):
        if o.uid not in mine and (os.path.isdir(os.path.join(root, o.uid)) or os.path.isdir(os.path.join(root, o.uid + "-G"))):
            yield o


class Command(BaseCommand):
    help = "Supprime apercus/commandes NON PAYES et uploads de plus de N jours (conformite RGPD)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--production-days", type=int, default=60)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        days, dry = opts["days"], opts["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)
        orders_dir = os.path.join(settings.MEDIA_ROOT, "orders")
        uploads_dir = os.path.join(settings.MEDIA_ROOT, "uploads")
        limit = days * 86400

        # 1. Commandes abandonnees (jamais payees)
        stale = Order.objects.filter(created_at__lt=cutoff, status=Order.PENDING)
        n_orders = stale.count()
        stale_uids = set(stale.values_list("uid", flat=True))
        if not dry:
            stale.delete()

        # 2. Dossiers d'apercu : seulement s'ils n'appartiennent a aucune commande conservee
        #    ni a aucune toile numerique (Mes toiles / galerie)
        keep = set(Order.objects.values_list("uid", flat=True)) | set(DigitalCanvas.objects.values_list("uid", flat=True))
        n_dirs = 0
        if os.path.isdir(orders_dir):
            for name in os.listdir(orders_dir):
                p = os.path.join(orders_dir, name)
                base = name[:-2] if name.endswith("-G") else name
                if (not os.path.isdir(p) or name.startswith(PROTECTED_PREFIXES) or base in keep):
                    continue
                if base in stale_uids or (time.time() - os.path.getmtime(p)) > limit:
                    n_dirs += 1
                    if not dry:
                        shutil.rmtree(p, ignore_errors=True)

        # 3. Uploads bruts anciens
        n_up = 0
        if os.path.isdir(uploads_dir):
            for name in os.listdir(uploads_dir):
                p = os.path.join(uploads_dir, name)
                if os.path.isfile(p) and (time.time() - os.path.getmtime(p)) > limit:
                    n_up += 1
                    if not dry:
                        os.remove(p)

        # 4. Fichiers de fabrication des commandes expediees depuis plus de N jours (hors « Mes toiles »)
        n_prod = 0
        for o in production_expired(opts["production_days"]):
            n_prod += 1
            if not dry:
                for u in (o.uid, o.uid + "-G"):
                    shutil.rmtree(os.path.join(orders_dir, u), ignore_errors=True)
                OrderEvent.objects.create(order=o, kind="action", user="purge",
                                          text=PURGED_TEXT % opts["production_days"])

        # 5. Mesure d'audience : 13 mois
        if not dry:
            from studio import analytics
            analytics.purge(13)

        mode = "simulation" if dry else "supprime"
        self.stdout.write(self.style.SUCCESS(
            f"[purge_old_data] commandes non payees: {n_orders}, apercus orphelins: {n_dirs}, "
            f"uploads: {n_up}, fichiers de fabrication: {n_prod} , {mode} (seuil {days} j / "
            f"{opts['production_days']} j apres expedition)."))
