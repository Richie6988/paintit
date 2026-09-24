import os
import shutil
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from studio.models import DigitalCanvas

OLD = "gal-gal-"
LIB = "__library__"


class Command(BaseCommand):
    help = ("Renomme les uid de galerie 'gal-gal-xxx' en 'gal-xxx' (DB + dossiers media) et supprime "
            "les anciens modeles 'lib-*' (doublons de la galerie). Idempotent.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        root = os.path.join(settings.MEDIA_ROOT, "orders")
        self._purge_lib(root, dry)
        olds = sorted(set(DigitalCanvas.objects.filter(uid__startswith=OLD).values_list("uid", flat=True)))
        if not olds:
            self.stdout.write(self.style.SUCCESS("Rien a renommer.")); return
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

    def _purge_lib(self, root, dry):
        """Anciens modeles 'lib-*' (dossier samples/, doublon de gallery/) : ligne bibliotheque
        supprimee ; dossier media supprime seulement si aucun joueur ne possede la toile."""
        libs = sorted(set(DigitalCanvas.objects.filter(email=LIB, uid__startswith="lib-")
                          .values_list("uid", flat=True)))
        if os.path.isdir(root):
            libs = sorted(set(libs) | {u for u in os.listdir(root) if u.startswith("lib-")})
        for uid in libs:
            owned = DigitalCanvas.objects.filter(uid=uid).exclude(email=LIB).exists()
            self.stdout.write("purge %s%s" % (uid, " (garde les fichiers : possede par un joueur)" if owned else ""))
            if dry:
                continue
            DigitalCanvas.objects.filter(email=LIB, uid=uid).delete()
            d = os.path.join(root, uid)
            if not owned and os.path.isdir(d):
                shutil.rmtree(d)
