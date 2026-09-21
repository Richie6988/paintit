#!/usr/bin/env python3
"""
pbn.py : Générateur "peinture par numéros" (paint-by-numbers).

Entrée : une image + dimension du support + nombre de couleurs.
Sortie : un SVG (contours de zones + numéros), une légende palette (PNG),
         un aperçu colorié (PNG).

Approche hybride :
  - Cœur CLASSIQUE (par défaut, sans GPU) : lissage -> quantification couleur
    K-means en espace LAB -> nettoyage des petites zones -> contours OpenCV
    -> placement des numéros -> export SVG.
  - CNN OPTIONNEL (--cnn hed) : HED (Holistically-Nested Edge Detection) via
    cv2.dnn, utilisé pour un lissage préservant les contours d'objets avant la
    quantification. Graceful fallback si les poids du modèle sont absents.

Dépendances : numpy, opencv-python(-headless), scikit-learn, scipy
"""
import argparse
import json
import os
import sys
import uuid
import numpy as np
import cv2

try:
    import segno
except ImportError:
    segno = None
from sklearn.cluster import KMeans
from scipy import ndimage


# --------------------------------------------------------------------------- #
# Utilitaires géométrie / unités
# --------------------------------------------------------------------------- #
def target_working_size(src_w, src_h, width_cm, height_cm, dpi):
    """Retourne (W_px, H_px, mm_par_px_x, mm_par_px_y, w_mm, h_mm)."""
    w_mm = width_cm * 10.0
    if height_cm is None:
        h_mm = w_mm * src_h / src_w
    else:
        h_mm = height_cm * 10.0
    W = max(1, int(round(w_mm / 25.4 * dpi)))
    H = max(1, int(round(h_mm / 25.4 * dpi)))
    return W, H, w_mm / W, h_mm / H, w_mm, h_mm


# --------------------------------------------------------------------------- #
# CNN optionnel : HED
# --------------------------------------------------------------------------- #
class _CropLayer:
    """Couche 'Crop' requise par le modèle HED sous cv2.dnn."""
    def __init__(self, params, blobs):
        self.xstart = self.ystart = self.xend = self.yend = 0

    def getMemoryShapes(self, inputs):
        a, b = inputs[0], inputs[1]
        h, w = b[2], b[3]
        self.ystart = (a[2] - h) // 2
        self.xstart = (a[3] - w) // 2
        self.yend, self.xend = self.ystart + h, self.xstart + w
        return [[a[0], a[1], h, w]]

    def forward(self, inputs):
        return [inputs[0][:, :, self.ystart:self.yend, self.xstart:self.xend]]


def hed_edges(bgr, model_dir):
    """Carte de contours HED normalisée [0..1], ou None si le modèle est absent."""
    proto = os.path.join(model_dir, "deploy.prototxt")
    weights = os.path.join(model_dir, "hed_pretrained_bsds.caffemodel")
    if not (os.path.isfile(proto) and os.path.isfile(weights)):
        print(f"[HED] modèle introuvable dans {model_dir} -> fallback classique.",
              file=sys.stderr)
        return None
    try:
        cv2.dnn_registerLayer("Crop", _CropLayer)
    except cv2.error:
        pass
    net = cv2.dnn.readNetFromCaffe(proto, weights)
    H, W = bgr.shape[:2]
    blob = cv2.dnn.blobFromImage(bgr, 1.0, (W, H),
                                 (104.007, 116.669, 122.679), swapRB=False, crop=False)
    net.setInput(blob)
    edge = net.forward()[0, 0]
    edge = cv2.resize(edge, (W, H))
    return np.clip(edge, 0, 1).astype(np.float32)


# --------------------------------------------------------------------------- #
# Lissage + quantification
# --------------------------------------------------------------------------- #
DETAIL = {
    "low":  dict(ms_sp=25, ms_sr=45, eps=1.6, mode_k=5, mode_iter=2),
    "med":  dict(ms_sp=18, ms_sr=35, eps=1.0, mode_k=3, mode_iter=1),
    "high": dict(ms_sp=11, ms_sr=25, eps=0.6, mode_k=3, mode_iter=1),
}


def smooth(bgr, mode, detail, edge=None):
    p = DETAIL[detail]
    if mode == "none":
        out = bgr.copy()
    elif mode == "bilateral":
        out = cv2.bilateralFilter(bgr, 9, 75, 75)
    else:  # meanshift (défaut)
        out = cv2.pyrMeanShiftFiltering(bgr, p["ms_sp"], p["ms_sr"])
    if edge is not None:
        # Blend préservant les contours : on garde l'original là où HED voit un bord.
        a = edge[..., None]
        out = (out.astype(np.float32) * (1 - a) + bgr.astype(np.float32) * a)
        out = out.astype(np.uint8)
    return out


def quantize(bgr, k, seed=0, sample=60000):
    """K-means en LAB. Retourne (labels HxW int, palette_bgr Kx3 uint8).
    Ajusté sur un échantillon (rapide) puis prédit sur toute l'image."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    Z = lab.reshape(-1, 3).astype(np.float32)
    km = KMeans(n_clusters=k, random_state=seed, n_init=3)
    if len(Z) > sample:
        idx = np.random.RandomState(seed).choice(len(Z), sample, replace=False)
        km.fit(Z[idx])
        labels = km.predict(Z).reshape(bgr.shape[:2])
    else:
        km.fit(Z)
        labels = km.labels_.reshape(bgr.shape[:2])
    centers_lab = km.cluster_centers_.astype(np.uint8).reshape(-1, 1, 3)
    palette_bgr = cv2.cvtColor(centers_lab, cv2.COLOR_LAB2BGR).reshape(-1, 3)
    return labels, palette_bgr


# --------------------------------------------------------------------------- #
# Nettoyage des petites zones
# --------------------------------------------------------------------------- #
SHIFTS = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]


def mode_filter(labels, k, ksize=3, iterations=1):
    """Lisse la carte de labels par vote majoritaire dans une fenetre KxK.
    Supprime les pixels isoles et les escaliers, sans melanger les couleurs."""
    if iterations <= 0:
        return labels
    out = labels
    for _ in range(iterations):
        best = None
        bestc = np.zeros(out.shape, dtype=out.dtype)
        for c in range(k):
            cnt = cv2.boxFilter((out == c).astype(np.float32), -1, (ksize, ksize),
                                normalize=False, borderType=cv2.BORDER_REPLICATE)
            if best is None:
                best = cnt
            else:
                better = cnt > best
                bestc[better] = c
                best = np.where(better, cnt, best)
        out = bestc
    return out


def _reassign(labels, mask, k):
    """Repeuple les pixels 'mask' par le label du plus proche pixel connu
    (distance transform, O(N))."""
    if not mask.any():
        return labels
    if mask.all():
        return labels
    idx = ndimage.distance_transform_edt(
        mask, return_distances=False, return_indices=True)
    return labels[tuple(idx)]


def clean_small(labels, k, min_area, min_radius=0.0, passes=4):
    """Fusionne les zones trop petites en AIRE (min_area) ou trop FINES (rayon
    inscrit < min_radius, donc impeignables) dans leur voisin le plus proche."""
    for _ in range(passes):
        small = np.zeros(labels.shape, bool)
        for c in range(k):
            m = (labels == c)
            if not m.any():
                continue
            n, cc = cv2.connectedComponents(m.astype(np.uint8), connectivity=8)
            if n <= 1:
                continue
            counts = np.bincount(cc.ravel())
            dt = ndimage.distance_transform_edt(m) if min_radius > 0 else None
            for lb in range(1, n):
                too_small = counts[lb] < min_area
                if not too_small and min_radius > 0:
                    too_small = dt[cc == lb].max() < min_radius
                if too_small:
                    small |= (cc == lb)
        if not small.any():
            break
        labels = _reassign(labels, small, k)
    return labels


def count_zones(labels, k):
    """Nombre total de composantes connexes (zones a peindre)."""
    total = 0
    for c in range(k):
        m = (labels == c).astype(np.uint8)
        if m.any():
            n, _ = cv2.connectedComponents(m, connectivity=8)
            total += n - 1
    return total


def limit_zones(labels, k, min_area, max_zones, min_radius=0.0):
    """Plafonne le nombre de zones : augmente le seuil d'aire et refusionne
    jusqu'a passer sous max_zones (evite les milliers de micro-zones)."""
    area = int(min_area)
    for _ in range(16):
        if count_zones(labels, k) <= max_zones:
            break
        area = int(area * 1.6) + 2
        labels = clean_small(labels, k, area, min_radius=min_radius, passes=2)
    return labels


# --------------------------------------------------------------------------- #
# Contours + numéros -> SVG
# --------------------------------------------------------------------------- #
def label_at_center(mask):
    """Pôle d'inaccessibilité approx : max de la distance transform (sur bbox).
    On matelasse d'un bord de fond pour que le BORD de l'image compte comme
    une frontiere -> le chiffre reste a l'interieur (jamais coupe au bord)."""
    ys, xs = np.where(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub = mask[y0:y1, x0:x1]
    sub = np.pad(sub, 1, mode="constant", constant_values=0)
    dt = ndimage.distance_transform_edt(sub)
    i = np.argmax(dt)
    yy, xx = np.unravel_index(i, dt.shape)
    return x0 + (xx - 1), y0 + (yy - 1), dt[yy, xx]


def _trace_boundaries(labels):
    """Vectorise les frontieres entre zones en polylignes, chaque bord une seule
    fois (sur la grille des coins de pixels). Renvoie une liste de listes de
    points (x, y) en pixels ; les polylignes fermees ont premier point == dernier."""
    H, W = labels.shape
    stride = W + 1

    adj = {}

    def add(a, b):
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    # Cracks verticaux : entre pixels horizontalement voisins de labels differents.
    vy, vx = np.nonzero(labels[:, :-1] != labels[:, 1:])
    for y, x in zip(vy.tolist(), vx.tolist()):
        i = x + 1
        add(y * stride + i, (y + 1) * stride + i)
    # Cracks horizontaux : entre pixels verticalement voisins de labels differents.
    hy, hx = np.nonzero(labels[:-1, :] != labels[1:, :])
    for y, x in zip(hy.tolist(), hx.tolist()):
        j = y + 1
        add(j * stride + x, j * stride + x + 1)

    used = set()

    def canon(a, b):
        return (a, b) if a < b else (b, a)

    def walk(start, nxt):
        pts = [start, nxt]
        used.add(canon(start, nxt))
        cur = nxt
        while True:
            cont = [n for n in adj[cur] if canon(cur, n) not in used]
            if len(adj[cur]) == 2 and len(cont) == 1:
                nn = cont[0]
                used.add(canon(cur, nn))
                pts.append(nn)
                cur = nn
            else:
                break
        return pts

    lines = []
    # Chaines ouvertes : demarrent aux sommets qui ne sont pas de degre 2.
    for p in list(adj.keys()):
        if len(adj[p]) != 2:
            for n in adj[p]:
                if canon(p, n) not in used:
                    lines.append(walk(p, n))
    # Boucles fermees restantes (tous sommets de degre 2).
    for p in list(adj.keys()):
        for n in adj[p]:
            if canon(p, n) not in used:
                lines.append(walk(p, n))

    out = []
    for ids in lines:
        out.append([(pid % stride, pid // stride) for pid in ids])
    return out


def _chaikin(pts, closed, iters=2):
    """Arrondit une polyligne (corner-cutting de Chaikin) pour supprimer l'aspect
    crenele des frontieres issues de pixels."""
    P = [(float(x), float(y)) for x, y in pts]
    for _ in range(iters):
        if len(P) < 3:
            break
        Q = []
        n = len(P)
        if closed:
            for i in range(n):
                a = P[i]; b = P[(i + 1) % n]
                Q.append((0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]))
                Q.append((0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]))
        else:
            Q.append(P[0])
            for i in range(n - 1):
                a = P[i]; b = P[i + 1]
                Q.append((0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]))
                Q.append((0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]))
            Q.append(P[-1])
        P = Q
    return P


def build_svg(labels, k, mmx, mmy, w_mm, h_mm, eps, min_label_area,
              stroke_mm=0.25):
    paths = []
    texts = []
    texts_px = []

    # Frontieres entre zones : tracees une seule fois puis lissees (Chaikin).
    eps_px = max(0.8, eps)
    for poly in _trace_boundaries(labels):
        closed = len(poly) > 2 and poly[0] == poly[-1]
        pts = poly[:-1] if closed else poly
        if len(pts) < 2:
            continue
        arr = np.array(pts, dtype=np.int32).reshape(-1, 1, 2)
        approx = cv2.approxPolyDP(arr, eps_px, closed)
        base = [(float(p[0][0]), float(p[0][1])) for p in approx]
        if len(base) < 2:
            continue
        sm = _chaikin(base, closed, iters=3) if len(base) >= 3 else base
        d = "M " + " L ".join(f"{x*mmx:.2f} {y*mmy:.2f}" for x, y in sm)
        if closed:
            d += " Z"
        paths.append(d)

    # Cadre exterieur de la toile (bord du support), trace une fois.
    paths.append(
        f"M 0 0 L {w_mm:.2f} 0 L {w_mm:.2f} {h_mm:.2f} L 0 {h_mm:.2f} Z")

    # Numeros : un par composante connexe, dimensionne pour TENIR dans le cercle
    # inscrit de la zone (evite les chevauchements) ; zones trop petites -> pas de numero.
    LABEL_MIN_R_MM = 1.0
    FS_CAP_MM = 7.0
    for c in range(k):
        m = (labels == c).astype(np.uint8)
        if not m.any():
            continue
        ncc, cc = cv2.connectedComponents(m, connectivity=8)
        for lb in range(1, ncc):
            comp = cc == lb
            area = int(comp.sum())
            if area < min_label_area:
                continue
            x, y, r = label_at_center(comp)
            r_mm = r * (mmx + mmy) * 0.5
            if r_mm < LABEL_MIN_R_MM:
                continue
            num = c + 1
            n = len(str(num))
            # demi-diagonale du texte <= ~0.9*r : fs = 1.8*r / hypot(0.60*n, 0.62)
            denom = float(np.hypot(0.60 * n, 0.62))
            fs = float(min(FS_CAP_MM, 1.8 * r_mm / denom))
            if fs < 1.3:
                continue
            texts.append((x * mmx, y * mmy + fs * 0.34, fs, num))
            texts_px.append((x, y, r, num))

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        f'width="{w_mm:.1f}mm" height="{h_mm:.1f}mm" '
        f'viewBox="0 0 {w_mm:.2f} {h_mm:.2f}">',
        f'<rect x="0" y="0" width="{w_mm:.2f}" height="{h_mm:.2f}" fill="white"/>',
        f'<g fill="none" stroke="#111" stroke-width="{stroke_mm}" '
        f'stroke-linejoin="round" stroke-linecap="round">',
    ]
    svg += [f'<path d="{d}"/>' for d in paths]
    svg.append('</g>')
    svg.append('<g fill="#444" font-family="Arial, sans-serif" '
               'text-anchor="middle">')
    for x, y, fs, num in texts:
        n = len(str(num))
        halfw = 0.32 * n * fs
        cx = min(max(x, halfw + 0.5), w_mm - halfw - 0.5)
        cy = min(max(y, fs * 0.9), h_mm - fs * 0.25)
        svg.append(f'<text x="{cx:.2f}" y="{cy:.2f}" '
                   f'font-size="{fs:.2f}">{num}</text>')
    svg.append('</g></svg>')
    return "\n".join(svg), texts_px


# --------------------------------------------------------------------------- #
# Sorties bitmap : aperçu colorié + légende palette
# --------------------------------------------------------------------------- #
def build_digipaint_svg(labels, palette_bgr, mmx, mmy, w_mm, h_mm, eps=1.0):
    """SVG jouable AUTONOME : chaque zone = un <path> remplissable AVEC son propre trait
    noir fin (meme geometrie que le remplissage -> alignement parfait) + son numero.
    data-n = numero cible, data-a = surface (score)."""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_mm:.1f}mm" height="{h_mm:.1f}mm" '
           f'viewBox="0 0 {w_mm:.2f} {h_mm:.2f}" shape-rendering="geometricPrecision" '
           f'text-rendering="geometricPrecision">',
           f'<rect x="0" y="0" width="{w_mm:.2f}" height="{h_mm:.2f}" fill="#ffffff"/>']
    ap = max(0.5, eps * 0.5)
    LABEL_MIN_R_MM = 0.55
    FS_CAP = 7.0
    for c in range(len(palette_bgr)):
        m = (labels == c).astype(np.uint8)
        if not m.any():
            continue
        ncc, cc = cv2.connectedComponents(m, connectivity=8)
        for lb in range(1, ncc):
            comp = (cc == lb).astype(np.uint8)
            area = int(comp.sum())
            # dilate d'1px : le remplissage rejoint le trait inter-zone (pas de halo blanc)
            comp_fill = cv2.dilate(comp, np.ones((3, 3), np.uint8), iterations=1)
            cnts, _ = cv2.findContours(comp_fill, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
            segs = []
            for cnt in cnts:
                cnt = cv2.approxPolyDP(cnt, ap, True)
                if len(cnt) < 3:
                    continue
                pts = cnt.reshape(-1, 2)
                segs.append("M " + " L ".join(f"{x*mmx:.2f} {y*mmy:.2f}" for x, y in pts) + " Z")
            if not segs:
                continue
            num = c + 1
            txt = ""
            x, y, r = label_at_center(comp == 1)
            r_mm = r * (mmx + mmy) * 0.5
            if r_mm >= LABEL_MIN_R_MM:
                n = len(str(num))
                fs = float(min(FS_CAP, 2.05 * r_mm / float(np.hypot(0.58 * n, 0.60))))
                if fs >= 0.7:
                    hw = 0.32 * n * fs
                    cx = min(max(x * mmx, hw + 0.5), w_mm - hw - 0.5)
                    cy = min(max(y * mmy + fs * 0.34, fs * 0.9), h_mm - fs * 0.25)
                    txt = f'<text class="zn" x="{cx:.2f}" y="{cy:.2f}" font-size="{fs:.2f}">{num}</text>'
            out.append('<g class="cell"><path class="z" fill="#eef1f6" '
                       f'fill-rule="evenodd" data-n="{num}" data-a="{area}" '
                       f'd="{" ".join(segs)}"/>{txt}</g>')
    # UN SEUL trait (frontieres tracees une fois), lisse et assez epais pour couvrir le joint
    eps_px = max(0.8, eps)
    lines = []
    for poly in _trace_boundaries(labels):
        closed = len(poly) > 2 and poly[0] == poly[-1]
        pts = poly[:-1] if closed else poly
        if len(pts) < 2:
            continue
        arr = np.array(pts, dtype=np.int32).reshape(-1, 1, 2)
        approx = cv2.approxPolyDP(arr, eps_px, closed)
        base = [(float(pp[0][0]), float(pp[0][1])) for pp in approx]
        if len(base) < 2:
            continue
        sm = _chaikin(base, closed, iters=3) if len(base) >= 3 else base
        d = "M " + " L ".join(f"{x*mmx:.2f} {y*mmy:.2f}" for x, y in sm)
        if closed:
            d += " Z"
        lines.append(d)
    if lines:
        out.append('<g class="lines" fill="none" stroke="#141414" stroke-width="0.34" '
                   'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke">')
        out += [f'<path d="{d}"/>' for d in lines]
        out.append('</g>')
    out.append("</svg>")
    return "\n".join(out)


def render_preview(labels, palette_bgr):
    return palette_bgr[labels]


def build_preview_svg(labels, palette_bgr, mmx, mmy, w_mm, h_mm, eps=1.0):
    """Apercu colorie VECTORIEL (zones remplies) : net a toute echelle."""
    k = len(palette_bgr)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_mm:.1f}mm" '
           f'height="{h_mm:.1f}mm" viewBox="0 0 {w_mm:.2f} {h_mm:.2f}" '
           f'shape-rendering="geometricPrecision">',
           f'<rect x="0" y="0" width="{w_mm:.2f}" height="{h_mm:.2f}" fill="#ffffff"/>']
    ap = max(0.6, eps * 0.5)
    for c in range(k):
        m = (labels == c).astype(np.uint8)
        if not m.any():
            continue
        cnts, _ = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        segs = []
        for cnt in cnts:
            cnt = cv2.approxPolyDP(cnt, ap, True)
            if len(cnt) < 3:
                continue
            pts = cnt.reshape(-1, 2)
            segs.append("M " + " L ".join(f"{x*mmx:.2f} {y*mmy:.2f}" for x, y in pts) + " Z")
        if segs:
            b = palette_bgr[c]
            hexc = "#%02X%02X%02X" % (int(b[2]), int(b[1]), int(b[0]))
            out.append(f'<path fill="{hexc}" stroke="{hexc}" stroke-width="0.5" stroke-linejoin="round" fill-rule="evenodd" d="{" ".join(segs)}"/>')
    # UN SEUL trait (frontieres tracees une fois), lisse et assez epais pour couvrir le joint
    eps_px = max(0.8, eps)
    lines = []
    for poly in _trace_boundaries(labels):
        closed = len(poly) > 2 and poly[0] == poly[-1]
        pts = poly[:-1] if closed else poly
        if len(pts) < 2:
            continue
        arr = np.array(pts, dtype=np.int32).reshape(-1, 1, 2)
        approx = cv2.approxPolyDP(arr, eps_px, closed)
        base = [(float(pp[0][0]), float(pp[0][1])) for pp in approx]
        if len(base) < 2:
            continue
        sm = _chaikin(base, closed, iters=3) if len(base) >= 3 else base
        d = "M " + " L ".join(f"{x*mmx:.2f} {y*mmy:.2f}" for x, y in sm)
        if closed:
            d += " Z"
        lines.append(d)
    if lines:
        out.append('<g class="lines" fill="none" stroke="#141414" stroke-width="0.34" '
                   'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke">')
        out += [f'<path d="{d}"/>' for d in lines]
        out.append('</g>')
    out.append("</svg>")
    return "\n".join(out)


def render_template_raster(labels, texts_px):
    """Rendu bitmap du template (contours + numéros) pour l'affichage web.
    Réutilise les positions de numéros déjà calculées par build_svg."""
    H, W = labels.shape
    img = np.full((H, W, 3), 255, np.uint8)
    diff = np.zeros((H, W), bool)
    diff[:, :-1] |= labels[:, :-1] != labels[:, 1:]
    diff[:-1, :] |= labels[:-1, :] != labels[1:, :]
    img[diff] = (70, 70, 70)
    Hh, Ww = img.shape[:2]
    for x, y, r, num in texts_px:
        txt = str(num)
        font = cv2.FONT_HERSHEY_SIMPLEX
        (w0, h0), _ = cv2.getTextSize(txt, font, 1.0, 2)
        diag = max(1.0, float(np.hypot(w0, h0)))
        scale = float(np.clip((1.7 * r) / diag, 0.3, 3.0))
        th = 1 if scale < 0.9 else 2
        (tw, tht), _ = cv2.getTextSize(txt, font, scale, th)
        px = int(min(max(x - tw / 2, 2), Ww - tw - 2))
        py = int(min(max(y + tht / 2, tht + 2), Hh - 2))
        cv2.putText(img, txt, (px, py), font, scale, (130, 130, 130), th, cv2.LINE_AA)
    return img


def _qr_bgr(data, target_px, border=4):
    """Rend un QR code (segno) en image BGR de ~target_px, ou None si indispo."""
    if segno is None:
        print("[QR] 'segno' non installé (pip install segno) -> QR ignoré.",
              file=sys.stderr)
        return None
    m = np.array(segno.make(data, error="m").matrix, dtype=np.uint8)
    n = m.shape[0]
    mod = max(1, target_px // (n + 2 * border))
    full = n + 2 * border
    canvas = np.ones((full, full), np.uint8)          # 1 = clair
    canvas[border:border + n, border:border + n] = 1 - m  # modules sombres = 0
    big = np.kron(canvas, np.ones((mod, mod), np.uint8)) * 255
    return cv2.cvtColor(big, cv2.COLOR_GRAY2BGR)


def _text(img, txt, org, scale, color, thick, font=cv2.FONT_HERSHEY_SIMPLEX):
    cv2.putText(img, txt, org, font, scale, color, thick, cv2.LINE_AA)


def _fit_text(img, txt, org, scale, color, s, max_w, font=cv2.FONT_HERSHEY_SIMPLEX):
    """Écrit txt en réduisant l'échelle si nécessaire pour tenir dans max_w."""
    thick = max(1, s)
    while scale > 0.2:
        (w, _), _ = cv2.getTextSize(txt, font, scale, thick)
        if w <= max_w:
            break
        scale -= 0.05 * s
        thick = max(1, int(round(scale)))
    cv2.putText(img, txt, org, font, max(scale, 0.2), color, max(1, thick), cv2.LINE_AA)


def render_palette(palette_bgr, brand="PAINT BY NUMBERS", logo_path=None,
                   qr_data=None, uid=None, discount_text="", cols=4, scale=3):
    """Légende palette haute résolution, brandée, avec QR de remise + ID unique."""
    s = scale
    sw, sh, pad = 104 * s, 64 * s, 12 * s
    k = len(palette_bgr)
    rows = (k + cols - 1) // cols
    grid_w = cols * sw + (cols + 1) * pad
    header_h = 46 * s
    footer_h = 96 * s if qr_data else 0
    W = grid_w
    H = header_h + rows * sh + (rows + 1) * pad + footer_h
    img = np.full((H, W, 3), 255, np.uint8)
    ink = (40, 40, 40)

    # ---- Header : logo (gauche) + nom de marque ------------------------- #
    cv2.rectangle(img, (0, 0), (W, header_h), (28, 28, 28), -1)
    tx = pad
    if logo_path and os.path.isfile(logo_path):
        logo = cv2.imread(logo_path, cv2.IMREAD_UNCHANGED)
        if logo is not None:
            lh = header_h - 2 * (6 * s)
            lw = int(logo.shape[1] * lh / logo.shape[0])
            logo = cv2.resize(logo, (lw, lh), interpolation=cv2.INTER_AREA)
            y0 = 6 * s
            if logo.shape[2] == 4:  # alpha
                a = logo[:, :, 3:4] / 255.0
                roi = img[y0:y0 + lh, tx:tx + lw]
                img[y0:y0 + lh, tx:tx + lw] = (logo[:, :, :3] * a + roi * (1 - a)).astype(np.uint8)
            else:
                img[y0:y0 + lh, tx:tx + lw] = logo[:, :, :3]
            tx += lw + pad
    fs = 1.0 * s
    (twh), _ = cv2.getTextSize(brand, cv2.FONT_HERSHEY_DUPLEX, fs, 2 * s)
    _text(img, brand, (tx, header_h // 2 + twh[1] // 2), fs, (255, 255, 255),
          max(1, 2 * s // 2), cv2.FONT_HERSHEY_DUPLEX)

    # ---- Grille de swatches -------------------------------------------- #
    y_off = header_h
    for i, bgr in enumerate(palette_bgr):
        r, c = divmod(i, cols)
        x0 = pad + c * (sw + pad)
        y0 = y_off + pad + r * (sh + pad)
        col = tuple(int(v) for v in bgr)
        band = 30 * s
        cv2.rectangle(img, (x0, y0), (x0 + sw, y0 + band), col, -1)
        cv2.rectangle(img, (x0, y0), (x0 + sw, y0 + sh), (60, 60, 60), max(1, s))
        lum = 0.114 * bgr[0] + 0.587 * bgr[1] + 0.299 * bgr[2]
        tc = (20, 20, 20) if lum > 140 else (255, 255, 255)
        _text(img, str(i + 1), (x0 + 5 * s, y0 + 22 * s), 0.8 * s, tc, max(1, s))
        hexs = "#%02X%02X%02X" % (bgr[2], bgr[1], bgr[0])
        rgbs = "RVB %d,%d,%d" % (bgr[2], bgr[1], bgr[0])
        maxw = sw - 8 * s
        _fit_text(img, hexs, (x0 + 4 * s, y0 + band + 17 * s), 0.5 * s, ink, s, maxw)
        _fit_text(img, rgbs, (x0 + 4 * s, y0 + band + 30 * s), 0.42 * s, (90, 90, 90), s, maxw)

    # ---- Footer : QR remise récurrente + ID unique --------------------- #
    if qr_data:
        fy = H - footer_h
        cv2.line(img, (pad, fy + 4 * s), (W - pad, fy + 4 * s), (210, 210, 210), max(1, s))
        qr = _qr_bgr(qr_data, footer_h - 16 * s)
        if qr is not None:
            qh = qr.shape[0]
            qx, qy = pad, fy + 8 * s
            img[qy:qy + qh, qx:qx + qh] = qr
            tx2 = qx + qh + pad
            _text(img, "REMISE FIDELITE", (tx2, fy + 24 * s), 0.75 * s, ink,
                  max(1, s), cv2.FONT_HERSHEY_DUPLEX)
            if discount_text:
                _text(img, discount_text, (tx2, fy + 44 * s), 0.5 * s, (70, 70, 70), max(1, s))
            _text(img, "Scannez a chaque commande", (tx2, fy + 60 * s), 0.45 * s,
                  (120, 120, 120), max(1, s))
            if uid:
                _text(img, "ID: " + uid, (tx2, fy + 80 * s), 0.5 * s, ink, max(1, s))
    return img


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def run(args):
    src = cv2.imread(args.image)
    if src is None:
        sys.exit(f"Image illisible : {args.image}")
    sh, sw = src.shape[:2]
    W, H, mmx, mmy, w_mm, h_mm = target_working_size(
        sw, sh, args.width_cm, args.height_cm, args.dpi)
    max_px = getattr(args, "max_px", 0) or 0
    if max_px and max(W, H) > max_px:
        f = max_px / max(W, H)
        W, H = max(1, int(W * f)), max(1, int(H * f))
        mmx, mmy = w_mm / W, h_mm / H
    # Plus il y a de couleurs, plus on baisse la resolution de travail : moins de
    # fragments parasites et un rendu plus net (le physique en mm est preserve).
    cap_px = 0   # resolution geree par max_px (adaptee a la taille du canvas)
    if cap_px and max(W, H) > cap_px:
        f = cap_px / max(W, H)
        W, H = max(1, int(W * f)), max(1, int(H * f))
        mmx, mmy = w_mm / W, h_mm / H
    img = cv2.resize(src, (W, H), interpolation=cv2.INTER_AREA)
    print(f"[i] travail {W}x{H}px  support {w_mm/10:.1f}x{h_mm/10:.1f}cm  "
          f"({mmx*1000:.0f}µm/px)")

    prog = getattr(args, "progress", None)
    def _p(pct, label):
        if prog:
            try:
                prog(pct, label)
            except Exception:
                pass
    _p(8, "read")

    edge = None
    if args.cnn == "hed":
        edge = hed_edges(img, args.hed_model_dir)

    sm = smooth(img, args.smooth, args.detail, edge)
    labels, palette = quantize(sm, args.colors, args.seed)
    _p(32, "colors")

    # Lissage de la carte de labels (plus fort quand il y a beaucoup de couleurs).
    d = DETAIL[args.detail]
    mk = 5 if args.colors >= 24 else d["mode_k"]
    mi = 3 if args.colors >= 24 else d["mode_iter"]
    labels = mode_filter(labels, args.colors, ksize=mk, iterations=mi)

    min_mm2 = args.min_zone_mm ** 2
    min_area = max(4, int(round(min_mm2 / (mmx * mmy))))
    px_per_mm = 1.0 / mmx
    min_radius = max(1.0, getattr(args, "min_paint_mm", 0.6) * px_per_mm)
    labels = clean_small(labels, args.colors, min_area, min_radius=0.0, passes=3)
    labels = clean_small(labels, args.colors, min_area, min_radius=min_radius, passes=1)

    # Plafond du NOMBRE de zones (peignable) : dependant de la surface et des couleurs.
    area_cm2 = (w_mm * h_mm) / 100.0
    density = 0.20 + args.colors * 0.010          # zones/cm2 (etat de l'art : plus fin)
    if getattr(args, "density", None):
        density = float(args.density)
    max_zones = int(np.clip(area_cm2 * density, 240, 3000))
    _mz = getattr(args, "max_zones", None)
    if _mz:
        max_zones = int(np.clip(int(_mz), 2, 9999))
    labels = limit_zones(labels, args.colors, min_area, max_zones, min_radius=min_radius)
    print(f"[i] zones finales: {count_zones(labels, args.colors)} (plafond {max_zones})")
    _p(58, "zones")
    # Une zone doit pouvoir accueillir un chiffre lisible (~2mm).
    min_label_area = max(min_area, int(round((2.0 ** 2) / (mmx * mmy))))

    eps = DETAIL[args.detail]["eps"] * args.dpi / 150.0
    if args.colors >= 24:
        eps *= 1.6                                # lignes plus douces quand c'est dense
    svg, texts_px = build_svg(labels, args.colors, mmx, mmy, w_mm, h_mm, eps,
                              min_label_area, args.stroke_mm)
    _p(78, "canvas")

    base = args.out or os.path.splitext(os.path.basename(args.image))[0] + "_pbn"
    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    svg_path = os.path.join(outdir, base + "_template.svg")
    prev_path = os.path.join(outdir, base + "_preview.png")
    pal_path = os.path.join(outdir, base + "_palette.png")
    tpl_png_path = os.path.join(outdir, base + "_template.png")

    with open(svg_path, "w") as f:
        f.write(svg)
    preview_svg_path = os.path.join(outdir, base + "_preview.svg")
    with open(preview_svg_path, "w") as f:
        f.write(build_preview_svg(labels, palette, mmx, mmy, w_mm, h_mm, eps))
    digipaint_path = os.path.join(outdir, base + "_digipaint.svg")
    with open(digipaint_path, "w") as f:
        f.write(build_digipaint_svg(labels, palette, mmx, mmy, w_mm, h_mm, eps))
    cv2.imwrite(prev_path, render_preview(labels, palette))
    cv2.imwrite(tpl_png_path, render_template_raster(labels, texts_px))
    _p(90, "number")

    uid = args.uid or f"{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:4].upper()}"
    qr_data = None
    if not args.no_qr:
        sep = "&" if "?" in args.discount_url else "?"
        qr_data = f"{args.discount_url}{sep}id={uid}"
    cv2.imwrite(pal_path, render_palette(
        palette, brand=args.brand, logo_path=args.logo, qr_data=qr_data,
        uid=uid, discount_text=args.discount_text, scale=args.pal_scale))

    n_zones = sum(cv2.connectedComponents((labels == c).astype(np.uint8), 8)[0] - 1
                  for c in range(args.colors))

    colors_path = os.path.join(outdir, base + "_colors.json")
    colors = [{"number": i + 1,
               "hex": "#%02X%02X%02X" % (int(bgr[2]), int(bgr[1]), int(bgr[0])),
               "rgb": [int(bgr[2]), int(bgr[1]), int(bgr[0])]}
              for i, bgr in enumerate(palette)]
    with open(colors_path, "w", encoding="utf-8") as f:
        json.dump(colors, f, ensure_ascii=False, indent=2)
    _p(98, "final")

    print(f"[✓] {n_zones} zones · {args.colors} couleurs")
    print(f"    {svg_path}\n    {prev_path}\n    {pal_path}")
    return svg_path, prev_path, pal_path, tpl_png_path, colors_path, preview_svg_path, digipaint_path


def build_parser():
    p = argparse.ArgumentParser(description="Génère un template peinture par numéros (SVG).")
    p.add_argument("image", help="image d'entrée")
    p.add_argument("-k", "--colors", type=int, default=12, help="nombre de couleurs (défaut 12)")
    p.add_argument("--width-cm", type=float, default=30.0, help="largeur du support en cm")
    p.add_argument("--height-cm", type=float, default=None, help="hauteur en cm (sinon ratio conservé)")
    p.add_argument("--dpi", type=int, default=150, help="résolution de travail (défaut 150)")
    p.add_argument("--max-px", type=int, default=0, help="plafond du plus grand côté en px (0 = sans limite)")
    p.add_argument("--min-zone-mm", type=float, default=3.0, help="taille min d'une zone peignable (mm)")
    p.add_argument("--min-paint-mm", type=float, default=0.6, help="rayon inscrit min d'une zone (mm)")
    p.add_argument("--detail", choices=list(DETAIL), default="med", help="niveau de détail")
    p.add_argument("--smooth", choices=["meanshift", "bilateral", "none"], default="meanshift")
    p.add_argument("--cnn", choices=["none", "hed"], default="none", help="module CNN optionnel")
    p.add_argument("--hed-model-dir", default="./hed", help="dossier des poids HED")
    p.add_argument("--stroke-mm", type=float, default=0.25, help="épaisseur des traits (mm)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=None, help="préfixe de sortie")
    p.add_argument("--outdir", default=".", help="dossier de sortie")
    # --- Branding & remise (légende palette) ---
    p.add_argument("--brand", default="PAINT BY NUMBERS", help="nom de marque (header palette)")
    p.add_argument("--logo", default=None, help="logo à afficher dans le header (PNG, alpha ok)")
    p.add_argument("--pal-scale", type=int, default=3, help="facteur de résolution de la palette")
    p.add_argument("--discount-url", default="https://exemple.com/remise",
                   help="URL de base encodée dans le QR (l'ID unique y est ajouté)")
    p.add_argument("--discount-text", default="-15% sur votre prochaine commande",
                   help="texte de remise affiché près du QR")
    p.add_argument("--uid", default=None, help="ID unique imposé (sinon généré)")
    p.add_argument("--no-qr", action="store_true", help="désactiver le QR de remise")
    return p


if __name__ == "__main__":
    run(build_parser().parse_args())
