import os, re, glob
from django.core.management.base import BaseCommand
from django.conf import settings
from studio.pipeline import generate
from studio.models import DigitalCanvas

LIB = "__library__"
# (fichier source, titre, categorie, uid, couleurs, slug_showcase_home)
SAMPLES = [
    ("rose.jpg",     "Rose du matin",       "Fleurs",    "lib-fleurs-1",    24, "rose"),
    ("cerisier.jpg", "Cerisiers en fleurs", "Fleurs",    "lib-fleurs-2",    30, "cerisier"),
    ("foret.jpg",    "Foret lumineuse",     "Paysage",   "lib-paysage-1",   24, "foret"),
    ("aquarium.jpg", "Aquarium tropical",   "Animaux",   "lib-animaux-1",   36, "aquarium"),
    ("chien.jpg",    "Beagle",              "Animaux",   "lib-animaux-2",   24, "chien"),
    ("moto.jpg",     "Moto de course",      "Vehicules", "lib-vehicules-1", 24, "moto"),
    ("people.jpg",   "People",              "People",    "lib-people-1",    2, "people"),
    ("pop-art.jpg",  "pop-art",             "pop-art",   "lib-pop-art-1",   12, "pop-art"),
    ("cat.jpg",      "cat",         "cat", "lib-cat-1", 12, "cat"),
    ("children.jpg", "children",            "children",  "lib-children-1",  12, "children"),
]


def _render_showcase(uid, slug, showdir):
    """Assets du home, sans dependance systeme : on copie/redimensionne les PNG deja
    produits par le pipeline (colorie = _preview.png, numerote vide = _template.png)."""
    from PIL import Image
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    pairs = [(uid + "_preview.png", slug + "_pbn.png"),
             (uid + "_template.png", slug + "_template.png")]
    for src_name, out_name in pairs:
        src = os.path.join(d, src_name)
        if not os.path.exists(src):
            continue
        out = os.path.join(showdir, out_name)
        try:
            im = Image.open(src).convert("RGB")
            w = 720
            h = max(1, int(im.height * w / im.width))
            im.resize((w, h)).save(out, quality=85)
        except Exception:
            import shutil
            shutil.copy(src, out)


class Command(BaseCommand):
    help = "Genere la bibliotheque (store) ET les assets d'animation du home, en phase."

    def handle(self, *args, **opts):
        base = os.path.join(settings.BASE_DIR, "studio", "static", "studio", "samples")
        showdir = os.path.join(settings.BASE_DIR, "studio", "static", "studio", "showcase")
        os.makedirs(showdir, exist_ok=True)
        n = 0
        for fname, title, cat, uid, colors, slug in SAMPLES:
            src = os.path.join(base, fname)
            if not os.path.exists(src):
                self.stdout.write(self.style.WARNING("Manquant: %s" % src)); continue
            self.stdout.write("Generation %s (%s)..." % (title, cat))
            generate(src, colors, 40, 50, uid=uid)
            DigitalCanvas.objects.update_or_create(
                email=LIB, uid=uid,
                defaults=dict(title=title, category=cat, colors=colors,
                              orientation="portrait", width_cm=40, height_cm=50, source="library"))
            try:
                _render_showcase(uid, slug, showdir)
            except Exception as e:
                self.stdout.write(self.style.WARNING("Assets home ignores pour %s : %s" % (slug, e)))
            n += 1
        self.stdout.write(self.style.SUCCESS(
            "Store + assets home generes : %d modeles. (Pense a collectstatic en prod.)" % n))
