import json
import os
import re
import numpy as np
import cv2
from django.conf import settings
from django.core.management.base import BaseCommand

import pbn


class Command(BaseCommand):
    help = ("Regenere les <uid>_digipaint.svg (jeu /paint) avec la geometrie a aretes partagees "
            "(plus de trous entre zones, coins de toile carres). Les zones sont reconstruites a "
            "partir de <uid>_preview.png + <uid>_colors.json : aucun recalcul du pipeline, "
            "numerotation et ordre des zones inchanges (progression des joueurs conservee).")

    def add_arguments(self, parser):
        parser.add_argument("uids", nargs="*", help="uid a traiter (defaut : tous)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        root = os.path.join(settings.MEDIA_ROOT, "orders")
        uids = opts["uids"] or (sorted(os.listdir(root)) if os.path.isdir(root) else [])
        ok = skip = 0
        for uid in uids:
            d = os.path.join(root, uid)
            svg_p = os.path.join(d, f"{uid}_digipaint.svg")
            prev_p = os.path.join(d, f"{uid}_preview.png")
            col_p = os.path.join(d, f"{uid}_colors.json")
            if not all(os.path.exists(p) for p in (svg_p, prev_p, col_p)):
                skip += 1
                continue
            try:
                old = open(svg_p, encoding="utf-8").read()
                m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', old[:400])
                w_mm, h_mm = float(m.group(1)), float(m.group(2))
                colors = json.load(open(col_p, encoding="utf-8"))
                img = cv2.imread(prev_p, cv2.IMREAD_COLOR)
                H, W = img.shape[:2]
                pal = np.array([[c["rgb"][2], c["rgb"][1], c["rgb"][0]] for c in colors], np.int32)
                # preview = palette[labels] (PNG sans perte) -> couleur la plus proche = label
                flat = img.reshape(-1, 3).astype(np.int32)
                labels = np.empty(len(flat), np.int32)
                for i in range(0, len(flat), 200000):
                    blk = flat[i:i + 200000]
                    labels[i:i + 200000] = ((blk[:, None, :] - pal[None]) ** 2).sum(2).argmin(1)
                labels = labels.reshape(H, W)
                mmx, mmy = w_mm / W, h_mm / H
                dpi = W * 25.4 / w_mm
                eps = pbn.DETAIL["med"]["eps"] * dpi / 150.0 * (1.6 if len(colors) >= 24 else 1.0)
                self.stdout.write(f"{uid}: {W}x{H}px, {len(colors)} couleurs")
                if opts["dry_run"]:
                    continue
                svg = pbn.build_digipaint_svg(labels, pal, mmx, mmy, w_mm, h_mm, eps)
                n_old, n_new = old.count('class="z"'), svg.count('class="z"')
                if n_old != n_new:   # la progression des joueurs est indexee par zone
                    self.stdout.write(self.style.WARNING(
                        "%s: nb de zones different (%d -> %d), ignore" % (uid, n_old, n_new)))
                    skip += 1
                    continue
                tmp = svg_p + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(svg)
                os.replace(tmp, svg_p)
                ok += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"{uid}: echec ({e})"))
        self.stdout.write(self.style.SUCCESS(f"{ok} SVG regeneres, {skip} dossiers ignores."))
