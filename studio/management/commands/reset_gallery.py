import os
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand

from studio.models import DigitalCanvas


class Command(BaseCommand):
    help = ("Vide entierement la GALERIE et le SLIDER du home pour repartir de zero : modeles de "
            "galerie (base), dossiers media/orders/gal-*, images sources (code + admin) et assets du home. "
            "Les commandes clients et leurs toiles personnelles ne sont pas touchees.")

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Confirme la suppression (sinon : simulation).")

    def handle(self, *args, **opts):
        go = opts["yes"]
        static = os.path.join(settings.BASE_DIR, "studio", "static", "studio")
        dirs = [os.path.join(static, "gallery"), os.path.join(static, "showcase"),
                os.path.join(settings.MEDIA_ROOT, "gallery"), os.path.join(settings.MEDIA_ROOT, "showcase")]
        if settings.STATIC_ROOT:
            dirs += [os.path.join(settings.STATIC_ROOT, "studio", "gallery"),
                     os.path.join(settings.STATIC_ROOT, "studio", "showcase")]
        orders = os.path.join(settings.MEDIA_ROOT, "orders")
        gal_dirs = [os.path.join(orders, d) for d in (os.listdir(orders) if os.path.isdir(orders) else [])
                    if d.startswith(("gal-", "lib-"))]
        lib = DigitalCanvas.objects.filter(email="__library__")
        owned = DigitalCanvas.objects.exclude(email="__library__").filter(uid__startswith="gal-")

        self.stdout.write("Modeles de galerie (base)      : %d" % lib.count())
        self.stdout.write("Copies galerie dans Mes toiles : %d" % owned.count())
        self.stdout.write("Dossiers media/orders/gal-*    : %d" % len(gal_dirs))
        for d in dirs:
            n = sum(len(f) for _r, _d, f in os.walk(d)) if os.path.isdir(d) else 0
            self.stdout.write("Fichiers %-22s: %d" % (os.path.relpath(d, settings.BASE_DIR), n))
        if not go:
            self.stdout.write(self.style.WARNING("Simulation : relancer avec --yes pour supprimer."))
            return

        lib.delete()
        owned.delete()
        for d in gal_dirs:
            shutil.rmtree(d, ignore_errors=True)
        for d in dirs:   # on vide le CONTENU : le dossier garde son proprietaire (utilisateur du site)
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                p = os.path.join(d, name)
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.stdout.write(self.style.WARNING(
                "Lance en root : si des dossiers media/ ont ete crees par root, rendez-les au site :\n"
                "  sudo chown -R paintit:paintit %s" % settings.MEDIA_ROOT))
        self.stdout.write(self.style.SUCCESS("Galerie et slider vides. Ajoutez vos modeles depuis Admin > Catalogue."))
