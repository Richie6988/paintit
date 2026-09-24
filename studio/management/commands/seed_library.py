import os
import re
from django.core.management.base import BaseCommand
from django.conf import settings
from studio.pipeline import generate
from studio.models import DigitalCanvas

LIB = "__library__"
EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def _slugify(text):
    import unicodedata
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()   # é -> e
    s = re.sub(r"[^A-Za-z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "img"


def _humanize(stem):
    t = re.sub(r"[_-]+", " ", stem).strip()
    return t[:1].upper() + t[1:] if t else stem


def gallery_dir():
    """Images de galerie livrees avec le code (depot git)."""
    return os.path.join(settings.BASE_DIR, "studio", "static", "studio", "gallery")


def showcase_dir():
    return os.path.join(settings.BASE_DIR, "studio", "static", "studio", "showcase")


def media_gallery_dir():
    """Images ajoutees depuis l'admin : DONNEES du site (media/, sauvegardees avec lui, hors git)."""
    return os.path.join(settings.MEDIA_ROOT, "gallery")


def media_showcase_dir():
    """Assets du home des modeles ajoutes depuis l'admin : servis tout de suite (pas de collectstatic)."""
    return os.path.join(settings.MEDIA_ROOT, "showcase")


def gallery_sources():
    """[(dossier galerie, dossier assets home)] : code puis admin."""
    return [(gallery_dir(), showcase_dir()), (media_gallery_dir(), media_showcase_dir())]


def item_for(full, base, prefix="gal-", default_colors=24):
    """Un fichier de la galerie -> item. Sous-dossier = categorie ; nom = titre ;
    suffixes : _cNN = couleurs, _pNNN = prix en centimes, _land = paysage (50x40)."""
    rel = os.path.relpath(full, base)
    parts = rel.replace("\\", "/").split("/")
    category = _humanize(parts[0]) if len(parts) >= 2 else "Galerie"
    stem = os.path.splitext(os.path.basename(full))[0]
    colors = default_colors
    m = re.search(r"_c(\d+)", stem)
    if m:
        colors = max(2, min(99, int(m.group(1)))); stem = stem.replace(m.group(0), "")
    price = 0.0
    pm = re.search(r"_p(\d+)", stem)
    if pm:
        price = round(int(pm.group(1)) / 100.0, 2); stem = stem.replace(pm.group(0), "")
    landscape = bool(re.search(r"_land\b", stem))
    stem = re.sub(r"_land\b", "", stem)
    if stem.lower().startswith(prefix):   # 'gal-rose.jpg' -> uid 'gal-rose' (pas 'gal-gal-rose')
        stem = stem[len(prefix):]
    stem = stem.strip("_-") or "modele"
    clean_rel = "/".join(parts[:-1] + [stem])   # chemin sans suffixes
    return dict(path=full, title=_humanize(stem), category=category, uid=prefix + _slugify(clean_rel),
                colors=colors, slug=_slugify(stem), price=price, landscape=landscape)


def seed_item(it, showdir=None, log=None):
    """Genere le modele + fiche bibliotheque + assets du home. Titre, categorie et prix ne sont
    fixes qu'a la creation : les modifications faites dans l'admin survivent a un nouveau seed."""
    w, h = (50, 40) if it.get("landscape") else (40, 50)
    generate(it["path"], it["colors"], w, h, uid=it["uid"])
    m, _ = DigitalCanvas.objects.update_or_create(
        email=LIB, uid=it["uid"],
        defaults=dict(colors=it["colors"], orientation="paysage" if it.get("landscape") else "portrait",
                      width_cm=w, height_cm=h, source="library", showcase_slug=it["slug"]),
        create_defaults=dict(title=it["title"], category=it["category"], price=it["price"], colors=it["colors"],
                             orientation="paysage" if it.get("landscape") else "portrait",
                             width_cm=w, height_cm=h, source="library", showcase_slug=it["slug"]))
    try:
        sd = showdir or showcase_dir()
        os.makedirs(sd, exist_ok=True)
        Command()._render_showcase(it["uid"], it["slug"], sd)
    except Exception as e:
        if log:
            log("Assets home ignores pour %s : %s" % (it["slug"], e))
    return m


def seed_image(src_path, title, category="", price=0, colors=24, landscape=False, in_slider=False):
    """Admin -> galerie : range l'image dans gallery/<Categorie>/ selon la convention de nommage
    (comme si on l'avait deposee a la main) puis la seed. -> fiche DigitalCanvas."""
    import shutil
    base = media_gallery_dir()
    folder = os.path.join(base, _slugify(category).replace("-", " ").title().replace(" ", "-")) if category else base
    os.makedirs(folder, exist_ok=True)
    ext = os.path.splitext(src_path)[1].lower()
    ext = ext if ext in EXTS else ".jpg"
    stem = "gal-" + _slugify(title)
    suffix = ("_c%d" % int(colors) if int(colors) != 24 else "") + ("_land" if landscape else "") + \
        "_p%03d" % int(round(float(price or 0) * 100))
    dst, n = os.path.join(folder, stem + suffix + ext), 2
    while os.path.exists(dst) or DigitalCanvas.objects.filter(
            uid=item_for(dst, base)["uid"]).exists():
        dst = os.path.join(folder, "%s-%d%s%s" % (stem, n, suffix, ext)); n += 1
    shutil.copyfile(src_path, dst)
    m = seed_item(item_for(dst, base, "gal-", int(colors)), media_showcase_dir())
    DigitalCanvas.objects.filter(pk=m.pk).update(title=title[:80], category=(category or "Galerie")[:40],
                                                 price=price or 0,   # libelles exacts (accents...)
                                                 in_slider=bool(in_slider))
    m.refresh_from_db()
    return m


def retire_model(uid):
    """Retire l'image source d'un modele de gallery/ (-> gallery/_retires/) : un futur seed_library
    ne le recree pas. -> chemin deplace ou None."""
    import shutil
    for base, _sd in gallery_sources():
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith("_")]
            for fn in files:
                full = os.path.join(root, fn)
                if fn.lower().endswith(EXTS) and item_for(full, base)["uid"] == uid:
                    arch = os.path.join(base, "_retires")
                    os.makedirs(arch, exist_ok=True)
                    dst = os.path.join(arch, fn)
                    shutil.move(full, dst)
                    return dst
    return None


class Command(BaseCommand):
    help = ("Genere la galerie (store) + les assets du home a partir de TOUTES les images "
            "de studio/static/studio/gallery/ (aucune liste en dur). "
            "Convention : sous-dossier = categorie ; nom de fichier = titre ; suffixes _cNN = nb couleurs, "
            "_pNNN = prix en centimes, _land = paysage. Dossiers '_xxx' (ex. _retires) ignores.")

    def add_arguments(self, parser):
        parser.add_argument("--colors", type=int, default=24,
                            help="Nombre de couleurs par defaut (surchargeable par _cNN dans le nom).")

    def handle(self, *args, **opts):
        root_static = os.path.join(settings.BASE_DIR, "studio", "static", "studio")
        gallery = os.path.join(root_static, "gallery")
        showdir = os.path.join(root_static, "showcase")
        os.makedirs(showdir, exist_ok=True)
        default_colors = int(opts["colors"])

        # gallery/ = source unique : modeles de la galerie (uid gal-xxx) ET assets du home
        items = []
        for gdir, sdir in gallery_sources():
            for it in self._scan(gdir, "gal-", default_colors):
                it["showdir"] = sdir
                items.append(it)
        if not items:
            self.stdout.write(self.style.WARNING("Aucune image dans %s ni %s" % (gallery, media_gallery_dir()))); return

        n = 0
        for it in items:
            tag = (", %.2f EUR" % it["price"]) if it["price"] else " (gratuit)"
            self.stdout.write("Generation %s (%s, %d couleurs%s)..." % (
                it["title"], it["category"], it["colors"], tag))
            try:
                seed_item(it, it.get("showdir") or showdir, log=self.stdout.write)
                n += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR("Echec %s : %s" % (it["title"], e)))

        self.stdout.write(self.style.SUCCESS(
            "Genere : %d modeles (galerie + assets du home). Pense a collectstatic." % n))

    def _scan(self, base, prefix, default_colors):
        """Scanne un dossier d'images -> liste d'items (voir item_for). Les dossiers "_xxx" sont ignores."""
        items = []
        if not os.path.isdir(base):
            return items
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith("_")]
            for fn in files:
                if fn.lower().endswith(EXTS):
                    items.append(item_for(os.path.join(root, fn), base, prefix, default_colors))
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
