import os
import re
from django.core.management.base import BaseCommand
from django.conf import settings
from studio.pipeline import generate
from studio.models import DigitalCanvas

LIB = "__library__"
EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def _slugify(text):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "img"


def _humanize(stem):
    t = re.sub(r"[_-]+", " ", stem).strip()
    return t[:1].upper() + t[1:] if t else stem


class Command(BaseCommand):
    help = ("Genere la bibliotheque (store) + les assets du home a partir de TOUTES les images "
            "de studio/static/studio/samples/ (aucune liste en dur). "
            "Convention : sous-dossier = categorie ; nom de fichier = titre ; suffixe _cNN = nb couleurs.")

    def add_arguments(self, parser):
        parser.add_argument("--colors", type=int, default=24,
                            help="Nombre de couleurs par defaut (surchargeable par _cNN dans le nom).")

    def handle(self, *args, **opts):
        root_static = os.path.join(settings.BASE_DIR, "studio", "static", "studio")
        samples = os.path.join(root_static, "samples")
        gallery = os.path.join(root_static, "gallery")
        showdir = os.path.join(root_static, "showcase")
        os.makedirs(showdir, exist_ok=True)
        default_colors = int(opts["colors"])

        # samples/ = HOMEPAGE uniquement (prefixe lib-) ; gallery/ = GALERIE uniquement (prefixe gal-)
        items = self._scan(samples, "lib-", default_colors, showcase=True)
        items += self._scan(gallery, "gal-", default_colors, showcase=False)
        if not items:
            self.stdout.write(self.style.WARNING(
                "Aucune image dans %s ni %s" % (samples, gallery))); return

        n = 0
        for it in items:
            tag = (", %.2f EUR" % it["price"]) if it["price"] else " (gratuit)"
            self.stdout.write("Generation %s (%s, %d couleurs%s)..." % (
                it["title"], it["category"], it["colors"], tag))
            try:
                generate(it["path"], it["colors"], 40, 50, uid=it["uid"])
                DigitalCanvas.objects.update_or_create(
                    email=LIB, uid=it["uid"],
                    defaults=dict(title=it["title"], category=it["category"], colors=it["colors"],
                                  orientation="portrait", width_cm=40, height_cm=50,
                                  source="library", price=it["price"]))
                if it["showcase"]:
                    try:
                        self._render_showcase(it["uid"], it["slug"], showdir)
                    except Exception as e:
                        self.stdout.write(self.style.WARNING(
                            "Assets home ignores pour %s : %s" % (it["slug"], e)))
                n += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR("Echec %s : %s" % (it["title"], e)))

        self.stdout.write(self.style.SUCCESS(
            "Genere : %d modeles (samples->homepage, gallery->galerie). Pense a collectstatic." % n))

    def _scan(self, base, prefix, default_colors, showcase):
        """Scanne un dossier d'images -> liste d'items. Sous-dossier = categorie ;
        nom = titre ; suffixe _cNN = couleurs ; suffixe _pNNN = prix en centimes."""
        items = []
        if not os.path.isdir(base):
            return items
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.lower().endswith(EXTS):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, base)
                parts = rel.replace("\\", "/").split("/")
                category = _humanize(parts[0]) if len(parts) >= 2 else ("Modeles" if showcase else "Galerie")
                stem = os.path.splitext(fn)[0]
                colors = default_colors
                m = re.search(r"_c(\d+)", stem)
                if m:
                    colors = max(2, min(99, int(m.group(1)))); stem = stem.replace(m.group(0), "")
                price = 0.0
                pm = re.search(r"_p(\d+)", stem)
                if pm:
                    price = round(int(pm.group(1)) / 100.0, 2); stem = stem.replace(pm.group(0), "")
                if not showcase and stem.lower().startswith(prefix):   # 'gal-rose.jpg' -> uid 'gal-rose' (pas 'gal-gal-rose')
                    stem = stem[len(prefix):]
                stem = stem.strip("_-") or "modele"
                clean_rel = "/".join(parts[:-1] + [stem])   # chemin sans suffixes _cNN/_pNNN
                slug = _slugify(clean_rel)
                items.append(dict(path=full, title=_humanize(stem), category=category,
                                  uid=prefix + slug, colors=colors, slug=_slugify(stem),
                                  price=price, showcase=showcase))
        return items

    def _render_showcase(self, uid, slug, showdir):
        """Assets du home (colorie + numerote), copies depuis les PNG du pipeline (Pillow)."""
        from PIL import Image
        d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
        for src_name, out_name in [(uid + "_preview.png", slug + "_pbn.png"),
                                   (uid + "_template.png", slug + "_template.png")]:
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
