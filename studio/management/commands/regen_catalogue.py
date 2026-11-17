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


class Command(BaseCommand):
    help = ("Regenere toutes les toiles du catalogue (galerie + slider) avec l'algorithme actuel, a partir de "
            "leur image source. Titre, categorie, prix, slider et visibilite sont conserves ; le nombre de zones "
            "est plafonne a l'ancien (meme difficulte). La progression des parties en cours sur ces toiles "
            "repart de zero (les zones changent). Option --set-prices : prix des modeles hors slider, "
            "ceux du slider passent gratuits.")

    def add_arguments(self, parser):
        parser.add_argument("uids", nargs="*", help="uid a traiter (defaut : tout le catalogue)")
        parser.add_argument("--dry-run", action="store_true", help="Liste sans rien modifier.")
        parser.add_argument("--set-prices", metavar="EUR", help="Ex. 0.50 : prix hors slider ; slider = gratuit.")
        parser.add_argument("--prices-only", action="store_true", help="Applique --set-prices sans regenerer.")

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
                old_zones = m.zones or _count_zones(m.uid)
                it.update(colors=int(m.colors or it["colors"]), landscape=landscape,
                          slug=m.showcase_slug or it["slug"],
                          density=(old_zones / (w * h) if old_zones else None), detail=1.0)
                try:
                    seed_item(it, sdir, log=self.stdout.write)
                except Exception as e:
                    self.stdout.write(self.style.ERROR("%s : echec (%s)" % (tag, e)))
                    fail += 1
                    continue
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
