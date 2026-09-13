"""Dossier de production d'une commande (au paiement).

Un SEUL fichier JSON complet par commande : media/orders/<uid>/order.json
(contient produit, prix, client, specs d'impression ET la liste couleurs -> numeros,
pour fabriquer le template OEM a la demande). Plus de colors.json / print.json /
supplier_order.json. Images conservees : <uid>_template.svg et <uid>_poster.png (A4).
"""
import datetime
import json
import math
import os

import cv2
import numpy as np
from django.conf import settings

try:
    import segno
except ImportError:
    segno = None

A4_W, A4_H = 2480, 3508
DPI = 300
MARGIN = 110
COLS = 6
STRIP_H = 360

FONT = cv2.FONT_HERSHEY_SIMPLEX
DUP = cv2.FONT_HERSHEY_DUPLEX

POSTER_STR = {
    "fr": {"disc": "REMISE FIDELITE", "sub": "-15% sur votre prochaine commande",
           "scan": "Scannez ou saisissez le code", "code": "Code : "},
    "en": {"disc": "LOYALTY DISCOUNT", "sub": "-15% off your next order",
           "scan": "Scan or enter the code", "code": "Code: "},
    "de": {"disc": "TREUE-RABATT", "sub": "-15% auf Ihre naechste Bestellung",
           "scan": "Code scannen oder eingeben", "code": "Code: "},
    "es": {"disc": "DESCUENTO FIDELIDAD", "sub": "-15% en tu proxima compra",
           "scan": "Escanea o introduce el codigo", "code": "Codigo: "},
}
FRENCH_COUNTRIES = {"France", "Belgique", "Suisse", "Luxembourg", "Canada",
                    "Cote d'Ivoire", "Senegal", "Maroc", "Tunisie"}
GERMAN_COUNTRIES = {"Allemagne", "Autriche"}
SPANISH_COUNTRIES = {"Espagne"}


def lang_for_country(country):
    if country in FRENCH_COUNTRIES:
        return "fr"
    if country in GERMAN_COUNTRIES:
        return "de"
    if country in SPANISH_COUNTRIES:
        return "es"
    return "en"


def _order_dir(uid):
    return os.path.join(settings.MEDIA_ROOT, "orders", uid)


def _fit_scale(txt, target_w, font, thick, start):
    s = start
    while s > 0.3:
        (w, _), _ = cv2.getTextSize(txt, font, s, thick)
        if w <= target_w:
            break
        s -= 0.05
    return s


def _qr(data, px):
    if segno is None:
        return None
    m = np.array(segno.make(data, error="m").matrix, dtype=np.uint8)
    n = m.shape[0]
    border = 3
    mod = max(1, px // (n + 2 * border))
    full = n + 2 * border
    canvas = np.ones((full, full), np.uint8)
    canvas[border:border + n, border:border + n] = 1 - m
    return cv2.cvtColor(np.kron(canvas, np.ones((mod, mod), np.uint8)) * 255, cv2.COLOR_GRAY2BGR)


def _logo():
    p = os.path.join(settings.BASE_DIR, "studio", "static", "studio", "img", "logo.png")
    return cv2.imread(p, cv2.IMREAD_UNCHANGED) if os.path.exists(p) else None


def _paste(dst, rgba, x, y):
    h, w = rgba.shape[:2]
    if y + h > dst.shape[0] or x + w > dst.shape[1]:
        return
    roi = dst[y:y + h, x:x + w]
    if rgba.shape[2] == 4:
        a = rgba[:, :, 3:4].astype(np.float32) / 255.0
        roi[:] = (rgba[:, :, :3].astype(np.float32) * a + roi.astype(np.float32) * (1 - a)).astype(np.uint8)
    else:
        roi[:] = rgba[:, :, :3]


def _compose_poster(uid, d, colors, lang):
    S = POSTER_STR.get(lang, POSTER_STR["en"])
    prev = cv2.imread(os.path.join(d, f"{uid}_preview.png"))
    img = np.full((A4_H, A4_W, 3), 255, np.uint8)
    mid = A4_H // 2

    # Haut : apercu (contain).
    box_w, box_h = A4_W - 2 * MARGIN, mid - MARGIN - 30
    ph, pw = prev.shape[:2]
    s = min(box_w / pw, box_h / ph)
    nw, nh = int(pw * s), int(ph * s)
    p = cv2.resize(prev, (nw, nh), interpolation=cv2.INTER_AREA)
    img[MARGIN + (box_h - nh) // 2:MARGIN + (box_h - nh) // 2 + nh, (A4_W - nw) // 2:(A4_W - nw) // 2 + nw] = p
    cv2.line(img, (MARGIN, mid), (A4_W - MARGIN, mid), (225, 225, 225), 2)

    # Palette.
    grid_top = mid + 40
    strip_top = A4_H - MARGIN - STRIP_H
    grid_bottom = strip_top - 30
    n = len(colors)
    rows = max(1, math.ceil(n / COLS))
    gx = gy = 20
    cell_w = (A4_W - 2 * MARGIN - (COLS - 1) * gx) // COLS
    cell_h = (grid_bottom - grid_top - (rows - 1) * gy) // rows
    hexh = min(74, int(cell_h * 0.26))
    block_h = cell_h - hexh
    for i, c in enumerate(colors):
        r, col = divmod(i, COLS)
        x = MARGIN + col * (cell_w + gx)
        y = grid_top + r * (cell_h + gy)
        b, g, rr = c["rgb"][2], c["rgb"][1], c["rgb"][0]
        cv2.rectangle(img, (x, y), (x + cell_w, y + block_h), (b, g, rr), -1)
        cv2.rectangle(img, (x, y), (x + cell_w, y + cell_h), (110, 110, 110), 2)
        lum = 0.114 * b + 0.587 * g + 0.299 * rr
        tc = (20, 20, 20) if lum > 140 else (255, 255, 255)
        ns = min(2.2, max(0.9, block_h / 130.0))
        cv2.putText(img, str(c["number"]), (x + 14, y + int(38 * ns)), FONT, ns, tc, 3, cv2.LINE_AA)
        hs = _fit_scale(c["hex"], cell_w - 16, FONT, 2, 1.0)
        cv2.putText(img, c["hex"], (x + 10, y + block_h + int(hexh * 0.72)), FONT, hs, (40, 40, 40), 2, cv2.LINE_AA)

    # Bandeau bas : logo a gauche, remise a droite.
    logo = _logo()
    if logo is not None:
        lw = 520
        lh = int(logo.shape[0] * lw / logo.shape[1])
        _paste(img, cv2.resize(logo, (lw, lh), interpolation=cv2.INTER_AREA),
               MARGIN, strip_top + (STRIP_H - lh) // 2)

    q = _qr(f"{settings.PBN_DISCOUNT_URL}?id={uid}", STRIP_H - 90)
    qh = q.shape[0] if q is not None else 0
    qx = A4_W - MARGIN - qh
    if q is not None:
        img[strip_top + 40:strip_top + 40 + qh, qx:qx + qh] = q
    right = qx - 40
    lines = [(S["disc"], DUP, 1.5, 3, (30, 30, 30), 90),
             (S["sub"], FONT, 0.95, 2, (70, 70, 70), 150),
             (S["scan"], FONT, 0.8, 2, (120, 120, 120), 200),
             (S["code"] + uid, FONT, 1.0, 2, (30, 30, 30), 260)]
    for txt, font, sc, th, color, dy in lines:
        (tw, _), _ = cv2.getTextSize(txt, font, sc, th)
        cv2.putText(img, txt, (right - tw, strip_top + dy), font, sc, color, th, cv2.LINE_AA)

    out = os.path.join(d, f"{uid}_poster.png")
    cv2.imwrite(out, img)


def build(order, shipping):
    """Ecrit order.json (complet) + poster A4, nettoie les intermediaires."""
    uid = order["uid"]
    d = _order_dir(uid)
    os.makedirs(d, exist_ok=True)

    # Couleurs : fournies par le preview (order["colors_list"]) ; fallback fichier si present.
    colors = order.get("colors_list") or []
    if not colors:
        cj = os.path.join(d, f"{uid}_colors.json")
        if os.path.exists(cj):
            with open(cj, encoding="utf-8") as f:
                colors = json.load(f)

    poster_lang = lang_for_country(shipping.get("country", ""))
    _compose_poster(uid, d, colors, poster_lang)

    disc = order.get("discount")
    order_json = {
        "uid": uid,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": "paid",
        "poster_lang": poster_lang,
        "supplier": settings.SUPPLIER_NAME,
        "product": {
            "type": "peinture_par_numeros",
            "format": order["format_label"], "orientation": order["orientation"],
            "width_cm": order["width_cm"], "height_cm": order["height_cm"],
            "colors": order["colors"],
            "mode": "dynamique, zone mini %.1f mm (~%.0f mm2)" % (
                order.get("min_zone_mm", 2.6), order.get("min_zone_mm", 2.6) ** 2),
            "brushes": bool(order.get("brushes")),
        },
        "pricing": {
            "currency": "EUR",
            "subtotal": round(order.get("canvas_price", order["price"] - order.get("brushes_amount", 0.0)), 2),
            "brushes_amount": order.get("brushes_amount", 0.0),
            "discount": ({"code": disc["code"], "percent": disc["percent"],
                          "amount": disc["amount"]} if disc else None),
            "total": order.get("total", order["price"]),
            "cost_estimate": order.get("cost", 0.0),
        },
        "customer": {
            "full_name": shipping["full_name"], "email": shipping["email"],
            "phone": f"{shipping.get('phone_code','')} {shipping.get('phone','')}".strip(),
            "address1": shipping["address1"], "address2": shipping.get("address2", ""),
            "postal_code": shipping["postal_code"], "city": shipping["city"],
            "country": shipping["country"],
        },
        "print": {
            "template_svg": {"file": f"{uid}_template.svg", "vector": True,
                             "width_mm": round(order["width_cm"] * 10, 1),
                             "height_mm": round(order["height_cm"] * 10, 1)},
            "poster": {"file": f"{uid}_poster.png", "paper": "A4",
                       "pixels": [A4_W, A4_H], "dpi": DPI},
        },
        "colors": colors,
    }
    with open(os.path.join(d, "order.json"), "w", encoding="utf-8") as f:
        json.dump(order_json, f, ensure_ascii=False, indent=2)

    # Nettoyage : un seul JSON, pas de doublons.
    for junk in (f"{uid}_colors.json", f"{uid}_palette.png"):
        pth = os.path.join(d, junk)
        if os.path.exists(pth):
            os.remove(pth)

    return {"dir": d, "files": [f"{uid}_template.svg", f"{uid}_poster.png", "order.json"]}
