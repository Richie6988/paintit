"""Purge RGPD : supprime commandes + fichiers de plus de N jours (defaut 30).

A planifier en cron/tache quotidienne, par ex. :
    python manage.py purge_old_data            # supprime > 30 jours
    python manage.py purge_old_data --days 30 --dry-run
"""
import os
import shutil
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from studio.models import Order


class Command(BaseCommand):
    help = "Supprime les commandes et fichiers de plus de N jours (conformite RGPD)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        days, dry = opts["days"], opts["dry_run"]
        cutoff = timezone.now() - timedelta(days=days)
        orders_dir = os.path.join(settings.MEDIA_ROOT, "orders")
        uploads_dir = os.path.join(settings.MEDIA_ROOT, "uploads")
        limit = days * 86400

        qs = Order.objects.filter(created_at__lt=cutoff)
        n_orders = qs.count()
        for od in qs:
            d = os.path.join(orders_dir, od.uid)
            if os.path.isdir(d) and not dry:
                shutil.rmtree(d, ignore_errors=True)
        if not dry:
            qs.delete()

        # Dossiers d'apercu (non payes) orphelins, d'apres la date de modif
        n_dirs = 0
        if os.path.isdir(orders_dir):
            for name in os.listdir(orders_dir):
                p = os.path.join(orders_dir, name)
                if os.path.isdir(p) and (time.time() - os.path.getmtime(p)) > limit:
                    n_dirs += 1
                    if not dry:
                        shutil.rmtree(p, ignore_errors=True)

        # Uploads bruts anciens
        n_up = 0
        if os.path.isdir(uploads_dir):
            for name in os.listdir(uploads_dir):
                p = os.path.join(uploads_dir, name)
                if os.path.isfile(p) and (time.time() - os.path.getmtime(p)) > limit:
                    n_up += 1
                    if not dry:
                        os.remove(p)

        mode = "dry-run (rien supprime)" if dry else "supprimes"
        self.stdout.write(self.style.SUCCESS(
            f"[purge_old_data] commandes: {n_orders}, dossiers orphelins: {n_dirs}, "
            f"uploads: {n_up} , {mode} (seuil {days} j)."))
