"""Fine couche entre Django et pbn.py."""
import os
import uuid
from types import SimpleNamespace

import cv2
from django.conf import settings

import pbn


def _rel_url(path):
    rel = os.path.relpath(path, settings.MEDIA_ROOT).replace("\\", "/")
    return settings.MEDIA_URL + rel


def new_uid():
    return f"{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:4].upper()}"


def _center_crop_to_ratio(src_path, dst_path, target_ratio, fx=0.5, fy=0.5):
    """Recadre au ratio largeur/hauteur du format autour d'un point focal.
    fx, fy in [0,1] : 0.5 = centre (defaut). Permet un recadrage manuel."""
    img = cv2.imread(src_path)
    if img is None:
        return src_path
    fx = min(1.0, max(0.0, float(fx)))
    fy = min(1.0, max(0.0, float(fy)))
    h, w = img.shape[:2]
    cur = w / h
    if cur > target_ratio:                      # trop large : on rogne les cotes
        nw = int(round(h * target_ratio))
        x0 = int(round((w - nw) * fx))
        x0 = max(0, min(w - nw, x0))
        img = img[:, x0:x0 + nw]
    elif cur < target_ratio:                    # trop haut : on rogne haut/bas
        nh = int(round(w / target_ratio))
        y0 = int(round((h - nh) * fy))
        y0 = max(0, min(h - nh, y0))
        img = img[y0:y0 + nh, :]
    long_side = max(img.shape[:2])
    cap = 1500
    if long_side > cap:
        f = cap / long_side
        img = cv2.resize(img, (int(img.shape[1] * f), int(img.shape[0] * f)),
                         interpolation=cv2.INTER_AREA)
    cv2.imwrite(dst_path, img)
    return dst_path


def _dynamic_min_zone(width_cm, height_cm, colors):
    """Plancher de zone adaptatif : peignable a la main ET fun.
    Depend du nombre de couleurs (plus de couleurs -> plus fin) et de la taille du
    canvas (plus grand -> zones un peu plus fines car il y a plus de place).
    Renvoie (min_zone_mm, min_paint_mm) ; l'aire mini = min_zone_mm**2."""
    import math
    base = {12: 2.3, 24: 1.8, 36: 1.5}.get(int(colors), 1.8)   # mm, cote mini de zone
    diag = math.hypot(float(width_cm), float(height_cm))
    scale = (64.0 / diag) ** 0.5                                # 40x50 (~64cm) = reference
    min_zone = max(1.4, min(3.6, base * scale))                # borne : 1.4 a 3.6 mm
    min_paint = round(min_zone * 0.35, 2)                      # filtre les slivers trop fins
    return round(min_zone, 2), min_paint



def _source_slug(uid, source_name):
    import re, os as _os
    base = _os.path.splitext(_os.path.basename(source_name or "image"))[0]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-")[:40] or "image"
    return "%s_source_%s.jpg" % (uid, slug)


def source_file(outdir, uid):
    import glob, os as _os
    m = sorted(glob.glob(_os.path.join(outdir, "%s_source_*" % uid)))
    if m:
        return m[0]
    legacy = _os.path.join(outdir, "source.jpg")
    return legacy if _os.path.exists(legacy) else ""


def generate(image_path, colors, width_cm, height_cm, uid=None, dpi=None, progress=None,
             focus=(0.5, 0.5), detail=1.0, source_name=None):
    """Pipeline peinture-par-numeros en mode dynamique : le plancher de zone
    s'adapte au nombre de couleurs et a la taille du canvas (peignable et fun).
    detail<1 = plus de zones (plus difficile) ; detail>1 = moins de zones."""
    uid = uid or new_uid()
    outdir = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    os.makedirs(outdir, exist_ok=True)

    cropped = os.path.join(outdir, _source_slug(uid, source_name or image_path))
    _center_crop_to_ratio(image_path, cropped, float(width_cm) / float(height_cm),
                          fx=focus[0], fy=focus[1])

    min_zone_mm, min_paint_mm = _dynamic_min_zone(width_cm, height_cm, colors)
    if detail and detail != 1.0:
        min_zone_mm = round(max(1.1, min_zone_mm * float(detail)), 2)
        min_paint_mm = round(min_zone_mm * 0.35, 2)
    ns = SimpleNamespace(
        image=cropped, colors=int(colors), width_cm=float(width_cm),
        height_cm=float(height_cm), dpi=int(dpi or settings.PBN_DPI),
        min_zone_mm=min_zone_mm, min_paint_mm=min_paint_mm,
        detail=("low" if int(colors) >= 24 else "med"), smooth="meanshift", cnn="none",
        hed_model_dir="./hed", stroke_mm=0.25, seed=0, out=uid, outdir=outdir,
        max_px=int(min(1400, max(1100, max(float(width_cm), float(height_cm)) * 20))),
        brand=settings.PBN_BRAND, logo=None, pal_scale=3,
        discount_url=settings.PBN_DISCOUNT_URL,
        discount_text="-15% sur votre prochaine commande",
        uid=uid, no_qr=False, progress=progress,
    )
    svg_path, prev_path, pal_path, tpl_png_path, colors_path, preview_svg_path, digipaint_path = pbn.run(ns)

    # On recupere la liste des couleurs puis on supprime les fichiers non exposes
    # (pas de colors.json ni palette.png qui trainent en backend).
    colors_list = []
    if colors_path and os.path.exists(colors_path):
        import json
        with open(colors_path, encoding="utf-8") as f:
            colors_list = json.load(f)
        # colors.json est conserve (reprise "Mes creations" + palette du jeu).
    if pal_path and os.path.exists(pal_path):
        os.remove(pal_path)

    return {
        "uid": uid,
        "preview_url": _rel_url(prev_path),
        "template_url": _rel_url(tpl_png_path),
        "colors_list": colors_list,
        "min_zone_mm": min_zone_mm,
    }


def pricing():
    from .models import Pricing
    return Pricing.get()


def price_cfg():
    from .forms import FORMATS
    p = pricing()
    keys = [k for k in FORMATS if p.format_available(k)] or list(FORMATS)
    cols = [n for n in (12, 24, 36) if getattr(p, "av_c%d" % n, True)] or [24]
    return {"formats": {k: p.format_price(k) for k in keys},
            "colors": {str(n): p.color_price(n) for n in cols},
            "brushes": p.brushes_price}


def compute_price(fmt_key, colors):
    """Prix = prix du format + supplement selon le nombre de couleurs."""
    p = pricing()
    return round(p.format_price(fmt_key) + p.color_price(colors), 2)
