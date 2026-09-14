"""
pipeline.py — Pipeline PBN découpé en étapes, pour Django.

Chaque étape :
  1. charge l'état du job (job_dir/state.npz + meta.json)
  2. calcule sa transformation
  3. sauvegarde une image de visualisation
  4. renvoie un dict {title, desc, stats, file} consommé par l'API.

Placez pbn.py (votre script) à côté de ce fichier.
"""
import os, json, uuid
import numpy as np
import cv2

import pbn  # pbn.py est a la racine du projet (deja sur le path Django)


# --------------------------------------------------------------------------- #
# Persistance du job
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, media_root, job_id=None):
        self.media_root = media_root
        self.id = job_id or uuid.uuid4().hex[:12]
        self.dir = os.path.join(media_root, "jobs", self.id)
        os.makedirs(self.dir, exist_ok=True)

    @property
    def state_path(self):
        return os.path.join(self.dir, "state.npz")

    @property
    def meta_path(self):
        return os.path.join(self.dir, "meta.json")

    def save(self, meta=None, **arrays):
        """Persiste meta.json + les tableaux numpy donnés (clé -> np.array)."""
        old = {}
        if os.path.isfile(self.state_path):
            with np.load(self.state_path) as z:
                old = {k: z[k] for k in z.files}
        old.update(arrays)
        np.savez_compressed(self.state_path, **old)
        if meta:
            m = self.load_meta()
            m.update(meta)
            with open(self.meta_path, "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False, indent=2)

    def load(self, *keys):
        with np.load(self.state_path) as z:
            return {k: z[k] for k in keys}

    def load_meta(self):
        if os.path.isfile(self.meta_path):
            with open(self.meta_path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def save_step_image(self, name, img_bgr):
        path = os.path.join(self.dir, f"{name}.png")
        cv2.imwrite(path, img_bgr)
        return f"jobs/{self.id}/{name}.png"

    def save_step_file(self, name, content):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"jobs/{self.id}/{name}"


# --------------------------------------------------------------------------- #
# Helpers visualisation
# --------------------------------------------------------------------------- #
def _label_view(labels, palette_bgr):
    """Carte de labels colorée avec la palette courante."""
    return palette_bgr[labels]


def _label_index_view(labels):
    """Carte des indices (numéros) en fausses couleurs + legende des index."""
    vis = (labels.astype(np.float32) / max(1, labels.max()) * 255).astype(np.uint8)
    return cv2.applyColorMap(vis, cv2.COLORMAP_TURBO)


# --------------------------------------------------------------------------- #
# ÉTAPES
# --------------------------------------------------------------------------- #
def step_upload(job, file_bytes, filename, params):
    """Étape 0 : réception de l'image originale."""
    nparr = np.frombuffer(file_bytes, np.uint8)
    src = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if src is None:
        raise ValueError("Image illisible (format non supporté).")
    h, w = src.shape[:2]
    job.save(meta={"params": params, "filename": filename, "orig_w": w, "orig_h": h},
             src=src)
    return {
        "key": "upload", "title": "1 · Upload",
        "desc": "Image originale décodée (BGR). C'est la seule donnée brute du pipeline.",
        "stats": [f"{w}×{h} px", f"{os.path.splitext(filename)[1]}",
                  f"{src.nbytes/1e6:.1f} Mo"],
        "file": job.save_step_image("01_upload", src),
    }


def step_resize(job, params):
    """Étape 1 : dimensionnement du support physique (mm -> px)."""
    src = job.load("src")["src"]
    h, w = src.shape[:2]
    W, H, mmx, mmy, w_mm, h_mm = pbn.target_working_size(
        w, h, params["width_cm"], params.get("height_cm") or None, params["dpi"])
    max_px = params.get("max_px") or 0
    note = ""
    if max_px and max(W, H) > max_px:
        f = max_px / max(W, H)
        W, H = max(1, int(W * f)), max(1, int(H * f))
        note = f" (plafonné à {max_px}px)"
    mmx, mmy = w_mm / W, h_mm / H
    img = cv2.resize(src, (W, H), interpolation=cv2.INTER_AREA)
    job.save(meta=dict(W=W, H=H, mmx=mmx, mmy=mmy, w_mm=w_mm, h_mm=h_mm),
             img=img)
    return {
        "key": "resize", "title": "2 · Cible physique",
        "desc": "Conversion cm/dpi en pixels de travail. mm/px sera utilisé pour toutes "
                "les tailles réelles (zones, traits, numéros).",
        "stats": [f"{W}×{H} px{note}", f"support {w_mm/10:.1f}×{h_mm/10:.1f} cm",
                  f"{mmx*1000:.0f} µm/px"],
        "file": job.save_step_image("02_resize", img),
    }


def step_smooth(job, params):
    """Étape 2 : lissage (mean-shift / bilateral / HED)."""
    img = job.load("img")["img"]
    edge = None
    cnn_note = ""
    if params.get("cnn") == "hed":
        edge = pbn.hed_edges(img, params.get("hed_model_dir", "./hed"))
        cnn_note = " + carte de contours HED (bords préservés)" if edge is not None else ""
    sm = pbn.smooth(img, params.get("smooth", "meanshift"), params["detail"], edge)
    job.save(sm=sm)
    return {
        "key": "smooth", "title": "3 · Lissage",
        "desc": f"Réduction du bruit avant quantification "
                f"({params.get('smooth','meanshift')}, détail={params['detail']}){cnn_note}.",
        "stats": [f"spatial={pbn.DETAIL[params['detail']]['ms_sp']}",
                  f"couleur={pbn.DETAIL[params['detail']]['ms_sr']}"],
        "file": job.save_step_image("03_smooth", sm),
    }


def step_quantize(job, params):
    """Étape 3 : K-means LAB -> labels + palette."""
    sm = job.load("sm")["sm"]
    k = params["colors"]
    labels, palette = pbn.quantize(sm, k, params.get("seed", 0))
    job.save(labels=labels.astype(np.int32), palette=palette)
    view = _label_view(labels, palette)
    return {
        "key": "quantize", "title": "4 · Quantification (K-means LAB)",
        "desc": f"K-means sur {k} clusters en espace LAB (perceptuel), ajusté sur "
                f"un échantillon puis prédit sur toute l'image.",
        "stats": [f"K={k}", f"palette {palette.shape[0]} couleurs",
                  f"{len(np.unique(labels))} labels utilisés"],
        "file": job.save_step_image("04_quantize", view),
    }


def step_modefilter(job, params):
    """Étape 4 : filtre modal (vote majoritaire) sur les labels."""
    z = job.load("labels", "palette")
    labels, palette = z["labels"], z["palette"]
    k = params["colors"]
    d = pbn.DETAIL[params["detail"]]
    mk = 5 if k >= 24 else d["mode_k"]
    mi = 3 if k >= 24 else d["mode_iter"]
    out = pbn.mode_filter(labels, k, ksize=mk, iterations=mi)
    job.save(labels=out.astype(np.int32))
    return {
        "key": "modefilter", "title": "5 · Filtre modal",
        "desc": "Vote majoritaire fenêtré K×K sur la carte de labels : supprime les "
                "pixels isolés et les escaliers sans mélanger les couleurs.",
        "stats": [f"fenêtre {mk}×{mk}", f"{mi} itération(s)",
                  f"zones avant/après : {pbn.count_zones(labels,k)} → {pbn.count_zones(out,k)}"],
        "file": job.save_step_image("05_modefilter", _label_index_view(out)),
    }


def step_clean(job, params):
    """Étape 5 : fusion des micro-zones."""
    z = job.load("labels", "palette")
    labels, palette = z["labels"], z["palette"]
    meta = job.load_meta()
    mmx, mmy = meta["mmx"], meta["mmy"]
    k = params["colors"]
    min_mm2 = params["min_zone_mm"] ** 2
    min_area = max(4, int(round(min_mm2 / (mmx * mmy))))
    px_per_mm = 1.0 / mmx
    min_radius = max(1.0, params.get("min_paint_mm", 0.6) * px_per_mm)
    before = pbn.count_zones(labels, k)
    labels = pbn.clean_small(labels, k, min_area, min_radius=0.0, passes=3)
    labels = pbn.clean_small(labels, k, min_area, min_radius=min_radius, passes=1)
    job.save(labels=labels.astype(np.int32))
    return {
        "key": "clean", "title": "6 · Nettoyage des micro-zones",
        "desc": "Chaque composante connexe plus petite que le seuil d'aire ou trop "
                "fine (rayon inscrit < seuil) est repeuplée par son voisin le plus "
                "proche (distance transform, O(N)).",
        "stats": [f"aire min {min_area} px ({params['min_zone_mm']} mm)",
                  f"rayon inscrit ≥ {min_radius:.1f} px",
                  f"zones : {before} → {pbn.count_zones(labels,k)}"],
        "file": job.save_step_image("06_clean", _label_view(labels, palette)),
    }


def step_limit(job, params):
    """Étape 6 : plafonnement du nombre de zones."""
    z = job.load("labels", "palette")
    labels, palette = z["labels"], z["palette"]
    meta = job.load_meta()
    mmx, mmy, w_mm, h_mm = meta["mmx"], meta["mmy"], meta["w_mm"], meta["h_mm"]
    k = params["colors"]
    area_cm2 = (w_mm * h_mm) / 100.0
    density = float(params.get("density") or (0.20 + k * 0.010))
    max_zones = int(np.clip(area_cm2 * density, 240, 3000))
    if params.get("max_zones"):
        max_zones = int(np.clip(params["max_zones"], 2, 9999))
    min_area = max(4, int(round(params["min_zone_mm"]**2 / (mmx*mmy))))
    px_per_mm = 1.0 / mmx
    min_radius = max(1.0, params.get("min_paint_mm", 0.6) * px_per_mm)
    before = pbn.count_zones(labels, k)
    labels = pbn.limit_zones(labels, k, min_area, max_zones, min_radius=min_radius)
    job.save(labels=labels.astype(np.int32))
    return {
        "key": "limit", "title": "7 · Plafond de zones",
        "desc": "Si le nombre de zones reste trop élevé pour être peignable, le seuil "
                "d'aire est augmenté itérativement et les micro-zones refusionnées.",
        "stats": [f"densité {density:.2f} zones/cm²",
                  f"plafond {max_zones}",
                  f"zones : {before} → {pbn.count_zones(labels,k)}"],
        "file": job.save_step_image("07_limit", _label_view(labels, palette)),
    }


def step_template(job, params):
    """Étape 7 : contours + numéros -> SVG + template raster."""
    z = job.load("labels", "palette")
    labels, palette = z["labels"], z["palette"]
    meta = job.load_meta()
    mmx, mmy, w_mm, h_mm = meta["mmx"], meta["mmy"], meta["w_mm"], meta["h_mm"]
    k = params["colors"]
    eps = pbn.DETAIL[params["detail"]]["eps"] * params["dpi"] / 150.0
    if k >= 24:
        eps *= 1.6
    min_area = max(4, int(round(params["min_zone_mm"]**2 / (mmx*mmy))))
    min_label_area = max(min_area, int(round((2.0**2) / (mmx*mmy))))
    svg, texts_px = pbn.build_svg(labels, k, mmx, mmy, w_mm, h_mm, eps,
                                  min_label_area, params.get("stroke_mm", 0.25))
    raster = pbn.render_template_raster(labels, texts_px)
    job.save(texts=np.array(texts_px, dtype=np.float32))
    svg_url = job.save_step_file("08_template.svg", svg)
    return {
        "key": "template", "title": "8 · Vectorisation (contours + numéros)",
        "desc": "Traçage des fissures inter-zones, simplification Douglas-Peucker, "
                "lissage de Chaikin, puis placement des numéros au pôle "
                "d'inaccessibilité (distance transform), dimensionnés pour tenir "
                "dans le cercle inscrit de chaque zone.",
        "stats": [f"{len(texts_px)} numéros placés",
                  f"ε={eps:.2f}px", f"trait {params.get('stroke_mm',0.25)} mm",
                  f"{len(svg)/1e3:.0f} ko SVG"],
        "file": job.save_step_image("08_template", raster),
        "extra_files": [{"name": "template.svg", "url": svg_url}],
    }


def step_preview(job, params):
    """Étape 8 : aperçu colorié (raster + SVG vectoriel)."""
    z = job.load("labels", "palette")
    labels, palette = z["labels"], z["palette"]
    meta = job.load_meta()
    mmx, mmy, w_mm, h_mm = meta["mmx"], meta["mmy"], meta["w_mm"], meta["h_mm"]
    eps = pbn.DETAIL[params["detail"]]["eps"] * params["dpi"] / 150.0
    prev = pbn.render_preview(labels, palette)
    prev_svg = pbn.build_preview_svg(labels, palette, mmx, mmy, w_mm, h_mm, eps)
    digi = pbn.build_digipaint_svg(labels, palette, mmx, mmy, w_mm, h_mm, eps)
    u1 = job.save_step_file("09_preview.svg", prev_svg)
    u2 = job.save_step_file("09_digipaint.svg", digi)
    return {
        "key": "preview", "title": "9 · Aperçu colorié",
        "desc": "Rendu de la carte de labels avec la palette finale — le résultat "
                "attendu une fois peint. Aussi exporté en SVG vectoriel + SVG "
                "'digipaint' jouable (zones cliquables).",
        "stats": [f"{params['colors']} couleurs", "SVG net à toute échelle"],
        "file": job.save_step_image("09_preview", prev),
        "extra_files": [{"name": "preview.svg", "url": u1},
                        {"name": "digipaint.svg", "url": u2}],
    }


def step_palette(job, params):
    """Étape 9 : légende palette brandée + QR."""
    z = job.load("palette",)
    palette = z["palette"]
    meta = job.load_meta()
    uid = f"{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:4].upper()}"
    qr_data = None
    if not params.get("no_qr"):
        sep = "&" if "?" in params["discount_url"] else "?"
        qr_data = f"{params['discount_url']}{sep}id={uid}"
    img = pbn.render_palette(
        palette, brand=params.get("brand", "PAINT BY NUMBERS"),
        logo_path=params.get("logo") or None, qr_data=qr_data, uid=uid,
        discount_text=params.get("discount_text", ""), scale=params.get("pal_scale", 3))
    # JSON couleurs
    colors = [{"number": i + 1,
               "hex": "#%02X%02X%02X" % (int(b[2]), int(b[1]), int(b[0])),
               "rgb": [int(b[2]), int(b[1]), int(b[0])]} for i, b in enumerate(palette)]
    cpath = job.save_step_file("10_colors.json", json.dumps(colors, indent=2))
    return {
        "key": "palette", "title": "10 · Légende palette",
        "desc": "Swatches numérotés haute résolution (hex + RVB), header brandé, "
                "QR de fidélité avec ID unique si activé.",
        "stats": [f"{len(palette)} couleurs", f"ID {uid}"],
        "file": job.save_step_image("10_palette", img),
        "extra_files": [{"name": "colors.json", "url": cpath}],
    }


# Ordre d'exécution
STEPS = [
    ("resize", step_resize), ("smooth", step_smooth),
    ("quantize", step_quantize), ("modefilter", step_modefilter),
    ("clean", step_clean), ("limit", step_limit),
    ("template", step_template), ("preview", step_preview),
    ("palette", step_palette),
]
