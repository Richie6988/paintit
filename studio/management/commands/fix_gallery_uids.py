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
        self._move_admin_uploads(dry)
        self._purge_lib(root, dry)
        self._convert_published(dry)
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

    def _convert_published(self, dry):
        """Modeles publies depuis une toile client AVANT le correctif (fiche bibliotheque pointant sur la
        toile du client : invisible en galerie, purgee avec la commande) -> vrai modele gal-<titre>."""
        from studio.views import _create_gallery_model
        for m in DigitalCanvas.objects.filter(email=LIB).exclude(uid__startswith="gal-"):
            self.stdout.write("publication %s -> modele gal- (%s)" % (m.uid, m.title or "sans titre"))
            if dry:
                continue
            try:
                _create_gallery_model(m.title or "Modele", m.category, m.price, colors=m.colors or 24,
                                      orientation=m.orientation, from_uid=m.uid)
                m.delete()
            except Exception as exc:
                self.stdout.write(self.style.ERROR("   echec : %s" % exc))

    def _move_admin_uploads(self, dry):
        """Images ajoutees depuis l'admin AVANT le correctif : rangees dans le dossier du code
        (fichiers non suivis par git) -> deplacees dans media/ (donnees du site)."""
        import subprocess
        from studio.management.commands.seed_library import (gallery_dir, showcase_dir,
                                                             media_gallery_dir, media_showcase_dir)
        base = str(settings.BASE_DIR)
        for src_root, dst_root in ((gallery_dir(), media_gallery_dir()), (showcase_dir(), media_showcase_dir())):
            if not os.path.isdir(src_root):
                continue
            try:
                tracked = set(subprocess.run(["git", "ls-files", "-z", "--", os.path.relpath(src_root, base)],
                                             cwd=base, capture_output=True, check=True).stdout.decode().split("\0"))
            except Exception:
                self.stdout.write(self.style.WARNING("git indisponible : deplacement des images admin ignore"))
                return
            for r, dirs, files in os.walk(src_root):
                for fn in files:
                    full = os.path.join(r, fn)
                    if os.path.relpath(full, base).replace(os.sep, "/") in tracked:
                        continue
                    dst = os.path.join(dst_root, os.path.relpath(full, src_root))
                    self.stdout.write("image admin -> media : %s" % os.path.relpath(full, base))
                    if not dry:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.move(full, dst)
            if not dry:   # dossiers vides laisses par le deplacement
                for r, dirs, files in sorted(os.walk(src_root), key=lambda x: -len(x[0])):
                    if r != src_root and not os.listdir(r):
                        os.rmdir(r)
