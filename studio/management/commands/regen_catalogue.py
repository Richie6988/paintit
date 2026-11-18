import os
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import F

from studio.models import CollabRoom, DigitalCanvas
from studio.management.commands.seed_library import LIB, EXTS, gallery_sources, item_for, seed_item


def _gallery_index():
    """uid -> (item, dossier assets du home) pour toutes les images sources de la galerie."""
    out = {}
    for base, sdir in gallery_sources():
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith("_")]
            for fn in files:
                if fn.lower().endswith(EXTS):
                    it = item_for(os.path.join(root, fn), base)
                    out.setdefault(it["uid"], (it, sdir))
    return out


def _count_zones(uid):
    from django.conf import settings
    f = os.path.join(settings.MEDIA_ROOT, "orders", uid, "%s_digipaint.svg" % uid)
    try:
        with open(f, encoding="utf-8") as fh:
            return fh.read().count('class="z"')
    except OSError:
        return 0


def _fidelity(uid):
    """Ecart de couleur moyen (DeltaE) entre la photo source et la toile coloriee : plus bas = plus fidele."""
    import cv2
    import numpy as np
    from django.conf import settings
    from studio.pipeline import source_file
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    src, prev = cv2.imread(source_file(d, uid) or ""), cv2.imread(os.path.join(d, "%s_preview.png" % uid))
    if src is None or prev is None:
        return None
    src = cv2.GaussianBlur(cv2.resize(src, (prev.shape[1], prev.shape[0]), interpolation=cv2.INTER_AREA), (0, 0), 1.5)
    lab = lambda x: cv2.cvtColor(x.astype(np.float32) / 255.0, cv2.COLOR_BGR2LAB)
    return float(np.linalg.norm(lab(src) - lab(prev), axis=2).mean())


def _cache_files():
    from django.conf import settings
    d = os.path.join(settings.MEDIA_ROOT, "cache", "pbn")
    try:
        return d, set(os.listdir(d))
    except OSError:
        return d, set()


def _purge_new_cache(before):
    """Le cache de calcul (~7 Mo par generation, garde 24 h) n'apporte rien a une regeneration du
    catalogue : on supprime les entrees creees pendant celle-ci, sinon des Go s'accumulent."""
    d, now = _cache_files()
    for f in now - before[1]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass


def _zone_cap(w, h, colors):
    """Plafond de zones du reglage auto (cf. pbn.run)."""
    return int(min(3000, max(240, w * h * (0.20 + colors * 0.010))))


EXPERT = (1.0, 0.6)          # = DENSITY_LEVELS["expert"] (densite zones/cm2, finesse)
CAP_HIT = 0.85               # plafond quasi atteint : le detail de l'image a ete bride
EXPERT_MIN_GAIN = 0.05       # « expert » conserve seulement s'il est >= 5 % plus fidele


class Command(BaseCommand):
    help = ("Regenere toutes les toiles du catalogue (galerie + slider) avec l'algorithme actuel, a partir de "
            "leur image source (rien n'est repris de l'ancien design), reglages optimises pour le rendu : nombre de "
            "couleurs recommande par la pre-analyse de l'image, densite « expert » si le plafond de zones bride le "
            "detail ET qu'elle est mesurablement plus fidele. Titre, categorie, prix, "
            "slider et visibilite sont conserves. Les parties en cours sur ces toiles repartent de zero. Option --set-prices : prix des modeles hors slider, "
            "ceux du slider passent gratuits.")

    def add_arguments(self, parser):
        parser.add_argument("uids", nargs="*", help="uid a traiter (defaut : tout le catalogue)")
        parser.add_argument("--dry-run", action="store_true", help="Liste sans rien modifier.")
        parser.add_argument("--set-prices", metavar="EUR", help="Ex. 0.50 : prix hors slider ; slider = gratuit.")
        parser.add_argument("--prices-only", action="store_true", help="Applique --set-prices sans regenerer.")
        parser.add_argument("--keep-colors", action="store_true", help="Garde le nombre de couleurs actuel de chaque modele.")
        parser.add_argument("--no-density", action="store_true", help="Densite auto, sans essai « expert ».")

    def handle(self, *args, **opts):
        qs = DigitalCanvas.objects.filter(email=LIB).order_by("uid")
        if opts["uids"]:
            qs = qs.filter(uid__in=opts["uids"])
        models = list(qs)
        price = Decimal(opts["set_prices"].replace(",", ".")).quantize(Decimal("0.01")) if opts["set_prices"] else None
        if opts["prices_only"] and price is None:
            self.stderr.write("--prices-only demande --set-prices.")
            return
        index = {} if opts["prices_only"] else _gallery_index()
        ok = fail = 0
        for i, m in enumerate(models, 1):
            new_price = (Decimal("0") if m.in_slider else price) if price is not None else m.price
            tag = "%s [%d/%d] %s, %d couleurs, %s%s -> prix %s EUR" % (
                m.uid, i, len(models), m.title or "-", m.colors, m.orientation,
                " (slider)" if m.in_slider else "", new_price)
            if opts["dry_run"]:
                self.stdout.write(tag + ("" if opts["prices_only"] or m.uid in index else "  !! image source introuvable"))
                continue
            if not opts["prices_only"]:
                src = index.get(m.uid)
                if not src:
                    self.stdout.write(self.style.ERROR(tag + " : image source introuvable dans gallery/, ignoree"))
                    fail += 1
                    continue
                it, sdir = dict(src[0]), src[1]
                landscape = m.orientation == "paysage"
                w, h = (50, 40) if landscape else (40, 50)
                old_zones, old_colors = m.zones or _count_zones(m.uid), m.colors
                colors = int(m.colors or it["colors"])
                if not opts["keep_colors"]:
                    colors = self._recommend(it["path"], w, h) or colors
                it.update(colors=colors, landscape=landscape, slug=m.showcase_slug or it["slug"], density=None, detail=1.0)
                before = _cache_files()
                try:
                    seed_item(it, sdir, log=self.stdout.write)
                    level = "auto"
                    zones, fid = _count_zones(m.uid), _fidelity(m.uid)
                    if not opts["no_density"] and fid is not None and zones >= CAP_HIT * _zone_cap(w, h, colors):
                        level = self._try_expert(it, sdir, m.uid, fid)
                except Exception as e:
                    self.stdout.write(self.style.ERROR("%s : echec (%s)" % (tag, e)))
                    fail += 1
                    continue
                finally:
                    _purge_new_cache(before)
                tag += ", %d -> %d couleurs, densite %s" % (old_colors, colors, level)
                m.refresh_from_db()
                m.zones = _count_zones(m.uid)
                m.save(update_fields=["zones"])
                # parties collaboratives : etat indexe par zone -> invalide
                CollabRoom.objects.filter(uid=m.uid).update(painted={}, version=F("version") + 1)
                tag += ", %d -> %d zones" % (old_zones, m.zones)
            if new_price != m.price:
                m.price = new_price
                m.save(update_fields=["price"])
            ok += 1
            self.stdout.write(self.style.SUCCESS(tag))
        if not opts["dry_run"]:
            self.stdout.write(self.style.SUCCESS("%d toile(s) traitee(s), %d echec(s)." % (ok, fail)))

    def _recommend(self, path, w, h):
        """Nombre de couleurs recommande (pre-analyse pbn.recommend_colors), parmi les options proposees."""
        import cv2
        import pbn
        from studio.forms import UploadForm
        from studio.pipeline import crop_to_ratio
        img = cv2.imread(path)
        if img is None:
            return None
        options = [int(v) for v, _l in UploadForm().fields["colors"].choices]
        return pbn.recommend_colors(crop_to_ratio(img, w / h), options)["best"]

    def _try_expert(self, it, sdir, uid, fid_auto):
        """Plafond de zones atteint : essai en densite « expert » ; conserve seulement si nettement plus fidele,
        sinon la version auto est restauree a l'identique. -> niveau retenu."""
        import shutil
        import tempfile
        from django.conf import settings
        from studio.management.commands.seed_library import Command as Seed
        d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
        keep = tempfile.mkdtemp()
        try:
            shutil.copytree(d, os.path.join(keep, "auto"))
            seed_item(dict(it, density=EXPERT[0], detail=EXPERT[1]), sdir, log=self.stdout.write)
            fid = _fidelity(uid)
            if fid is not None and fid <= fid_auto * (1 - EXPERT_MIN_GAIN):
                return "expert (fidelite %.2f -> %.2f)" % (fid_auto, fid)
            shutil.rmtree(d)
            shutil.copytree(os.path.join(keep, "auto"), d)
            Seed()._render_showcase(uid, it["slug"], sdir)
            return "auto (expert essaye, gain insuffisant)"
        finally:
            shutil.rmtree(keep, ignore_errors=True)
