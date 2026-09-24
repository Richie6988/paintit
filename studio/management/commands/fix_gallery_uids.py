import os
import shutil
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from studio.models import DigitalCanvas

OLD = "gal-gal-"


class Command(BaseCommand):
    help = "Renomme les uid de galerie 'gal-gal-xxx' en 'gal-xxx' (DB + dossiers media). Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        olds = sorted(set(DigitalCanvas.objects.filter(uid__startswith=OLD).values_list("uid", flat=True)))
        if not olds:
            self.stdout.write(self.style.SUCCESS("Rien a renommer.")); return
        root = os.path.join(settings.MEDIA_ROOT, "orders")
        for old in olds:
            new = old[4:]
            self.stdout.write("%s -> %s" % (old, new))
            if dry:
                continue
            # 1) fichiers : orders/<old>/<old>_*.ext -> orders/<new>/<new>_*.ext
            src, dst = os.path.join(root, old), os.path.join(root, new)
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                for fn in os.listdir(src):
                    nf = new + fn[len(old):] if fn.startswith(old) else fn
                    shutil.move(os.path.join(src, fn), os.path.join(dst, nf))
                os.rmdir(src)
            # 2) DB : toutes les lignes (bibliotheque + joueurs qui la possedent)
            with transaction.atomic():
                for dc in DigitalCanvas.objects.filter(uid=old):
                    if DigitalCanvas.objects.filter(email=dc.email, uid=new).exists():
                        dc.delete(); continue
                    dc.uid = new
                    if dc.email == "__library__" and dc.title.lower().startswith("gal "):
                        dc.title = dc.title[4:].capitalize()
                    dc.save(update_fields=["uid", "title"])
        self.stdout.write(self.style.SUCCESS("%d uid renommes." % (0 if dry else len(olds))))
