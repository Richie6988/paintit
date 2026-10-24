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
    "it": {"disc": "SCONTO FEDELTA", "sub": "-15% sul tuo prossimo ordine",
           "scan": "Scansiona o inserisci il codice", "code": "Codice: "},
}
FRENCH_COUNTRIES = {"France", "Belgique", "Suisse", "Luxembourg", "Canada",
                    "Cote d'Ivoire", "Senegal", "Maroc", "Tunisie"}
GERMAN_COUNTRIES = {"Allemagne", "Autriche"}
SPANISH_COUNTRIES = {"Espagne"}
ITALIAN_COUNTRIES = {"Italie", "Saint-Marin"}


def lang_for_country(country):
    if country in FRENCH_COUNTRIES:
        return "fr"
    if country in GERMAN_COUNTRIES:
        return "de"
    if country in SPANISH_COUNTRIES:
        return "es"
    if country in ITALIAN_COUNTRIES:
        return "it"
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

    # Tout en haut : n° de commande (rapprochement colis / toile chez le fournisseur).
    cv2.putText(img, uid, (MARGIN, MARGIN + 10), DUP, 1.6, (20, 20, 20), 3, cv2.LINE_AA)
    # Haut : apercu (contain).
    top = MARGIN + 50
    box_w, box_h = A4_W - 2 * MARGIN, mid - top - 30
    ph, pw = prev.shape[:2]
    s = min(box_w / pw, box_h / ph)
    nw, nh = int(pw * s), int(ph * s)
    p = cv2.resize(prev, (nw, nh), interpolation=cv2.INTER_AREA)
    img[top + (box_h - nh) // 2:top + (box_h - nh) // 2 + nh, (A4_W - nw) // 2:(A4_W - nw) // 2 + nw] = p
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


from .printfiles import PAD_MM
from .models import supplier_color_count
PAD_CM = PAD_MM / 10.0


def _print_size(oi):
    """Taille du fichier d'impression (toile + marge blanche des 2 cotes) : 40x50 -> 48x58 cm."""
    try:
        return "%g x %g cm" % (float(oi.get("width_cm")) + 2 * PAD_CM, float(oi.get("height_cm")) + 2 * PAD_CM)
    except (TypeError, ValueError):
        return "-"


def _supplier_sku(oi):
    from .order_lines import sku_for
    from .models import format_key
    try:
        return sku_for(format_key(oi.get("width_cm"), oi.get("height_cm")), oi.get("colors_count") or 24,
                       bool(oi.get("framed")))
    except Exception:
        return ""


def double_for_supplier(specs):
    """Kit fournisseur au minimum SUPPLIER_MIN_COLORS pots : 12 couleurs -> chaque couleur en 2 pots (meme numero
    sur la toile, surface partagee entre les 2). Au-dela, liste inchangee."""
    from .models import supplier_color_count
    n = len(specs)
    target = supplier_color_count(n)
    if not n or target == n:
        return specs
    out = []
    for i in range(target):
        c = dict(specs[i % n])
        reps = target // n + (1 if (i % n) < target % n else 0)
        for k in ("area_cm2", "area_pct"):
            if isinstance(c.get(k), (int, float)):
                c[k] = round(c[k] / reps, 2)
        out.append(c)
    return sort_by_code(out)


def supplier_text(sj):
    """Fiche fournisseur en texte simple (EN + 中文), meme contenu que <uid>_supplier.json, lisible sans outil :
    commande, destinataire, puis couleurs « numero -> reference fournisseur »."""
    oi, ci = sj.get("order_information", {}), sj.get("consignee_information", {})
    yn = lambda b: "YES / 是" if b else "NO / 否"
    phone = " ".join(x for x in (ci.get("phone_code", ""), ci.get("phone", "")) if x)
    L = ["PAINTIT ORDER / 订单  %s" % oi.get("uid", ""), "=" * 44,
         "Date / 日期:            %s" % (oi.get("created_at") or "")[:16].replace("T", " "),
         "Product / 产品:         Paint by numbers kit / 数字油画套装",
         "Size / 尺寸:            %s x %s cm (%s)" % (oi.get("width_cm"), oi.get("height_cm"), oi.get("orientation", "")),
         "Print file / 印刷尺寸:  %s (%g cm white border / %g厘米白边)" % (_print_size(oi), PAD_CM, PAD_CM),
         "Colors / 颜色数量:      %s" % oi.get("colors_count", ""),
         "Finish / 款式:          %s" % ("Framed / 带框 (BK)" if oi.get("framed") else "Unframed / 无框 (WF)"),
         "SKU:                    %s" % (oi.get("sku") or _supplier_sku(oi)),
         "Files / 文件:           %s" % ", ".join((oi.get("files") or {}).values()),
         "", "CONSIGNEE / 收货人", "-" * 44,
         "Name / 姓名:            %s" % ci.get("full_name", ""),
         "Phone / 电话:           %s" % phone,
         "Email / 邮箱:           %s" % ci.get("email", ""),
         "Address / 地址:         %s" % ci.get("address1", "")]
    if ci.get("address2"):
        L.append("                        %s" % ci["address2"])
    L += ["Postal code / 邮编:     %s" % ci.get("postal_code", ""),
          "City / 城市:            %s" % ci.get("city", ""),
          "Country / 国家:         %s" % ci.get("country", ""),
          ""]
    specs = sort_by_code(sj.get("color_specifications", []))
    L += ["COLORS / 颜色: %s colors, %sx%s cm" % (len(specs), oi.get("width_cm", ""), oi.get("height_cm", "")),
          "Sorted by factory color code (small -> large) / 按工厂色号从小到大", "",
          "%-22s %-18s %s" % ("Factory color code", "Number on canvas", "Color"),
          "%-22s %-18s %s" % ("工厂色号", "画布编号", "颜色"), "-" * 52]
    approx = False
    for c in specs:
        code = _fmt_code(c.get("supplier_code")) or "----"
        if c.get("supplier_code") and c.get("supplier_code_approx"):
            code += " *"; approx = True
        L.append("%-22s %-18s %s" % (code, c.get("number", ""), (c.get("hex") or "").upper()))
    if approx:
        L += ["", "* closest code in your color chart / 色卡中最接近的色号"]
    return "\n".join(L) + "\n"


def color_shares(uid, colors=None):
    """Part de surface a peindre de chaque couleur -> {numero: fraction 0..1}.
    Source : data-a (surface) des zones du SVG du jeu ; a defaut, pixels de l'apercu colorie."""
    import re
    from collections import defaultdict
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    tot = defaultdict(float)
    svg = os.path.join(d, "%s_digipaint.svg" % uid)
    if os.path.exists(svg):
        with open(svg, encoding="utf-8") as f:
            for n, a in re.findall(r'data-n="(\d+)" data-a="(\d+)"', f.read()):
                tot[int(n)] += float(a)
    elif colors and os.path.exists(os.path.join(d, "%s_preview.png" % uid)):
        img = cv2.imread(os.path.join(d, "%s_preview.png" % uid))
        if img is not None:
            img = cv2.resize(img, (200, int(200 * img.shape[0] / img.shape[1])), interpolation=cv2.INTER_NEAREST)
            px = img.reshape(-1, 3)[:, ::-1].astype(np.int32)          # BGR -> RGB
            pal = np.array([c.get("rgb") or [0, 0, 0] for c in colors], np.int32)
            near = np.argmin(((px[:, None, :] - pal[None, :, :]) ** 2).sum(-1), axis=1)
            for i, k in zip(*np.unique(near, return_counts=True)):
                tot[int(colors[i].get("number"))] += float(k)
    s = sum(tot.values())
    return {n: v / s for n, v in tot.items()} if s else {}


def add_areas(uid, specs, width_cm, height_cm, colors=None):
    """Ajoute surface (cm2, %) a chaque couleur ; liste triee par code couleur usine (format de l'atelier)."""
    shares = color_shares(uid, colors)
    if not shares:
        return sort_by_code(specs)
    canvas = float(width_cm or 0) * float(height_cm or 0)
    for c in specs:
        f = shares.get(int(c.get("number") or 0), 0.0)
        c["area_pct"] = round(f * 100, 2)
        if canvas:
            c["area_cm2"] = round(f * canvas, 1)
    return sort_by_code(specs)


def sort_by_code(specs):
    """Ordre de travail de l'atelier : code couleur usine croissant (sans code : a la fin, par numero)."""
    def key(c):
        code = str(c.get("supplier_code") or "")
        return (0, int(code), code) if code.isdigit() else (1 if code else 2, 0, code or "%06d" % (c.get("number") or 0))
    return sorted(specs, key=key)


def _fmt_code(code):
    code = str(code or "")
    return code.zfill(4) if code.isdigit() else code


def _font(size, bold=False):
    from PIL import ImageFont
    cands = (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else
             ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]) + \
        [os.path.join(settings.BASE_DIR, "studio", "static", "studio", "fonts", "Poppins-Bold.ttf")]
    for f in cands:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def supplier_colors_image(sj, path, title=""):
    """Tableau des couleurs (format de l'atelier) en JPG : code usine | numero sur la toile | pastille."""
    from PIL import Image, ImageDraw
    oi = sj.get("order_information", {})
    specs = sort_by_code(sj.get("color_specifications", []))
    W, row, top = 900, 56, 190
    H = top + row * len(specs) + 70
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    f_t, f_s, f_h, f_c, f_n = _font(34, True), _font(22), _font(24, True), _font(26), _font(26, True)
    d.text((36, 30), title or "Order %s (%s colors, %sx%scm)" % (oi.get("uid", ""), len(specs), oi.get("width_cm", ""),
                                                                 oi.get("height_cm", "")), font=f_t, fill="black")
    d.text((36, 82), "Sorted by factory color code (small -> large)", font=f_s, fill=(110, 110, 110))
    X = (36, 330, 650)
    for x, h in zip(X, ("Factory color code", "Number on canvas", "Color")):
        d.text((x, 136), h, font=f_h, fill="black")
    d.line([(24, top - 6), (W - 24, top - 6)], fill="black", width=2)
    approx = False
    for i, c in enumerate(specs):
        y = top + i * row
        if i % 2:
            d.rectangle([24, y, W - 24, y + row - 1], fill=(244, 244, 244))
        code = _fmt_code(c.get("supplier_code")) or "----"
        if c.get("supplier_code") and c.get("supplier_code_approx"):
            code += " *"; approx = True
        d.text((X[0], y + 14), code, font=f_c, fill="black")
        d.text((X[1] + 40, y + 14), str(c.get("number", "")), font=f_n, fill="black")
        rgb = tuple(c.get("rgb") or ()) or tuple(int((c.get("hex") or "#cccccc")[k:k + 2], 16) for k in (1, 3, 5))
        d.rectangle([X[2], y + 9, X[2] + 120, y + row - 9], fill=rgb, outline=(120, 120, 120))
    if approx:
        d.text((36, H - 50), "* closest code in your color chart", font=f_s, fill=(110, 110, 110))
    im.save(path, "JPEG", quality=92)
    return path


def write_supplier_text(uid, sj=None):
    """Ecrit <uid>_supplier.txt (depuis le JSON fournisseur s'il n'est pas fourni). -> chemin ou None."""
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    if sj is None:
        try:
            with open(os.path.join(d, "%s_supplier.json" % uid), encoding="utf-8") as f:
                sj = json.load(f)
        except (OSError, ValueError):
            return None
    specs = sj.get("color_specifications") or []
    if specs and "area_pct" not in specs[0]:
        oi = sj.get("order_information", {})
        sj["color_specifications"] = add_areas(uid, specs, oi.get("width_cm"), oi.get("height_cm"), specs)
    # anciennes fiches 12 couleurs : meme presentation que les nouvelles (kit 24, chaque couleur en double)
    sj["color_specifications"] = double_for_supplier(sj.get("color_specifications") or [])
    oi = sj.setdefault("order_information", {})
    if oi.get("colors_count"):
        oi["colors_count"] = supplier_color_count(oi["colors_count"])
    p = os.path.join(d, "%s_supplier.txt" % uid)
    with open(p, "w", encoding="utf-8") as f:
        f.write(supplier_text(sj))
    try:
        supplier_colors_image(sj, os.path.join(d, "%s_supplier_colors.jpg" % uid))   # meme tableau en image
    except Exception:
        pass
    return p


def _cjk_font(size):
    """Police chinoise si le serveur en a une (sinon None : la fiche reste en anglais)."""
    from PIL import ImageFont
    for f in ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
              "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
              os.path.join(settings.BASE_DIR, "studio", "static", "studio", "fonts", "NotoSansSC-Bold.ttf")):
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return None


def supplier_card(uid):
    """Image de la ligne de commande vue dans l'ERP du fournisseur : apercu du poster + QR + lien « ALL FILES ».
    L'ERP affiche cette image (et « 查看原图 ») : le lien vers tous les fichiers ne peut pas passer inapercu."""
    from PIL import Image, ImageDraw
    from .security import production_url
    d = _order_dir(uid)
    url = production_url(uid)
    W, H = 1200, 1500
    im = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(im)
    try:
        poster = Image.open(os.path.join(d, "%s_poster.png" % uid)).convert("RGB")
        poster.thumbnail((W - 80, 820))
        im.paste(poster, ((W - poster.width) // 2, 30))
    except OSError:
        pass
    y = 880
    dr.rectangle([0, y, W, H], fill=(255, 236, 140))
    cjk = _cjk_font(52)
    dr.text((40, y + 30), "ALL PRODUCTION FILES", font=_font(60, True), fill="black")
    if cjk:
        dr.text((40, y + 105), "全部生产文件（TIFF、色卡、订单信息）", font=cjk, fill="black")
    dr.text((40, y + 180), "TIFF + colour table + order sheet (ZIP)", font=_font(34), fill=(60, 60, 60))
    if cjk:
        dr.text((40, y + 240), "Scan the QR code or open: / 扫码或打开：", font=_cjk_font(34), fill=(60, 60, 60))
    else:
        dr.text((40, y + 240), "Scan the QR code or open:", font=_font(34), fill=(60, 60, 60))
    f_u = _font(30, True)
    line, lines = "", []
    for ch in url:                                   # URL coupee pour tenir a gauche du QR
        if dr.textlength(line + ch, font=f_u) > W - 480:
            lines.append(line); line = ""
        line += ch
    lines.append(line)
    for i, l in enumerate(lines):
        dr.text((40, y + 300 + i * 42), l, font=f_u, fill=(20, 60, 200))
    dr.text((40, H - 70), "Order %s" % uid, font=_font(36, True), fill="black")
    qr = _qr(url, 380)
    if qr is not None:
        q = Image.fromarray(cv2.cvtColor(qr, cv2.COLOR_BGR2RGB)).resize((380, 380), Image.NEAREST)
        im.paste(q, (W - 420, y + 200))
    out = os.path.join(d, "%s_supplier_card.jpg" % uid)
    im.save(out, "JPEG", quality=88)
    return out


def ensure_supplier_files(uid):
    """Fiche texte (EN/中文) et tableau des couleurs (JPG) presents pour la commande (crees depuis le JSON si besoin)."""
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    if not (os.path.exists(os.path.join(d, "%s_supplier.txt" % uid))
            and os.path.exists(os.path.join(d, "%s_supplier_colors.jpg" % uid))):
        try:
            write_supplier_text(uid)
        except Exception:
            pass
    if not os.path.exists(os.path.join(d, "%s_supplier_card.jpg" % uid)) and \
            os.path.exists(os.path.join(d, "%s_poster.png" % uid)):
        try:
            supplier_card(uid)
        except Exception:
            pass


def supplier_colors(colors):
    """Couleurs pour le FOURNISSEUR : numero de la toile -> code interne de SON nuancier + hex/RGB.
    Toile generee avant l'import du nuancier : code de la teinte la plus proche (CIEDE2000), marque « approx »."""
    pal = []
    try:
        from .pipeline import supplier_palette
        pal = supplier_palette() or []
    except Exception:
        pal = []
    out = []
    for c in colors:
        row = {"number": c.get("number")}
        code, approx = c.get("code") or c.get("supplier_code"), False
        if not code and pal and c.get("hex"):
            code, approx = _nearest_code(c["hex"], pal), True
        if code:
            row["supplier_code"] = str(code)
            if approx:
                row["supplier_code_approx"] = True
        row["hex"] = c.get("hex")
        row["rgb"] = c.get("rgb")
        out.append(row)
    return out


def _nearest_code(hex_, palette):
    import numpy as np
    import pbn
    h = hex_.lstrip("#")
    src = pbn._to_lab([[int(h[i:i + 2], 16) for i in (0, 2, 4)]])
    cand = [p for p in palette if p.get("hex")]
    lab = pbn._to_lab([[int(p["hex"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)] for p in cand])
    return str(cand[int(np.argmin(pbn.delta_e2000(src, lab)[0]))].get("code", ""))


def load_colors(uid):
    """Couleurs numerotees de la toile, sans jamais renvoyer vide si une source existe :
    <uid>_colors.json (pipeline) > order.json > supplier.json > palette deduite de l'apercu."""
    d = _order_dir(uid)
    cj = os.path.join(d, f"{uid}_colors.json")
    if os.path.exists(cj):
        with open(cj, encoding="utf-8") as f:
            cols = json.load(f)
        if cols:
            return cols
    for name in (f"{uid}_order.json", f"{uid}_supplier.json"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                cols = json.load(f).get("color_specifications") or []
            cols = [dict(c, code=c["supplier_code"]) if c.get("supplier_code") and not c.get("code") else c
                    for c in cols]
            if cols and all(c.get("hex") for c in cols):
                return sorted(cols, key=lambda c: c.get("number") or 0)   # fiche fournisseur : triee par surface
    # dernier recours : numero -> couleur relu sur la toile elle-meme : chaque numero imprime
    # sur la toile numerotee est au milieu d'une zone de SA couleur dans l'apercu (PNG sans perte).
    return colors_from_canvas(uid)
    return []


def colors_from_canvas(uid):
    import re
    from collections import Counter
    d = _order_dir(uid)
    tp, pv = os.path.join(d, f"{uid}_template.svg"), os.path.join(d, f"{uid}_preview.png")
    if not (os.path.exists(tp) and os.path.exists(pv)):
        return []
    svg = open(tp, encoding="utf-8").read()
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    img = cv2.imread(pv, cv2.IMREAD_COLOR)
    if not m or img is None:
        return []
    wmm, hmm = float(m.group(1)), float(m.group(2))
    H, W = img.shape[:2]
    votes = {}
    for x, y, fs, t in re.findall(r'<text x="([\d.]+)" y="([\d.]+)" font-size="([\d.]+)">(\d+)</text>', svg):
        cx = int(float(x) / wmm * W)
        cy = int((float(y) - 0.34 * float(fs)) / hmm * H)      # y = ligne de base -> centre du chiffre
        if 0 <= cx < W and 0 <= cy < H:
            b, g, r = (int(v) for v in img[cy, cx])
            votes.setdefault(int(t), Counter())[(r, g, b)] += 1
    cols = []
    for num in sorted(votes):
        r, g, b = votes[num].most_common(1)[0][0]
        cols.append({"number": num, "hex": "#%02X%02X%02X" % (r, g, b), "rgb": [r, g, b]})
    return cols


def build(order, shipping):
    """Ecrit order.json (complet) + poster A4, nettoie les intermediaires."""
    uid = order["uid"]
    d = _order_dir(uid)
    os.makedirs(d, exist_ok=True)

    # Couleurs : fournies par le preview (order["colors_list"]) ; fallback fichier si present.
    colors = order.get("colors_list") or load_colors(uid)
    if colors and not os.path.exists(os.path.join(d, f"{uid}_colors.json")):
        with open(os.path.join(d, f"{uid}_colors.json"), "w", encoding="utf-8") as f:   # restaure la source
            json.dump(colors, f, ensure_ascii=False, indent=2)

    poster_lang = lang_for_country(shipping.get("country", ""))
    _compose_poster(uid, d, colors, poster_lang)

    disc = order.get("discount")
    order_json = {
        "consignee_information": {
            "full_name": shipping["full_name"], "email": shipping["email"],
            "phone": ("%s %s" % (shipping.get("phone_code", ""), shipping.get("phone", ""))).strip(),
            "address1": shipping["address1"], "address2": shipping.get("address2", ""),
            "postal_code": shipping["postal_code"], "city": shipping["city"],
            "country": shipping["country"],
        },
        "order_information": {
            "uid": uid,
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "status": "paid", "poster_lang": poster_lang, "supplier": settings.SUPPLIER_NAME,
            "product_type": "peinture_par_numeros",
            "format": order["format_label"], "orientation": order["orientation"],
            "width_cm": order["width_cm"], "height_cm": order["height_cm"],
            "colors_count": order["colors"], "brushes": bool(order.get("brushes")),
            "framed": bool(order.get("framed")),
            "files": {
                "template_svg": "%s_template.svg" % uid,
                "template_tiff": "%s_template.tiff" % uid,
                "preview_svg": "%s_preview.svg" % uid,
                "poster_png": "%s_poster.png" % uid,
            },
            "pricing": {
                "currency": "EUR",
                "subtotal": round(order.get("canvas_price", order["price"] - order.get("brushes_amount", 0.0)), 2),
                "brushes_amount": order.get("brushes_amount", 0.0),
                "discount": ({"code": disc["code"], "percent": disc["percent"], "amount": disc["amount"]} if disc else None),
                "total": order.get("total", order["price"]),
            },
        },
        "color_specifications": [
            {"number": c.get("number"), "hex": c.get("hex"), "rgb": c.get("rgb"),
             **({"supplier_code": c["code"]} if c.get("code") else {})} for c in colors],
    }
    with open(os.path.join(d, "%s_order.json" % uid), "w", encoding="utf-8") as f:
        json.dump(order_json, f, ensure_ascii=False, indent=2)

    # Version FOURNISSEUR : uniquement ce qu'il faut pour produire et livrer
    # (ni statut, ni langue du poster, ni nom de fournisseur, ni type produit, ni prix).
    oi = order_json["order_information"]
    supplier_json = {
        "consignee_information": order_json["consignee_information"],
        "order_information": {
            "uid": uid, "created_at": oi["created_at"],
            "format": oi["format"], "orientation": oi["orientation"],
            "width_cm": oi["width_cm"], "height_cm": oi["height_cm"],
            "colors_count": supplier_color_count(oi["colors_count"]), "framed": oi.get("framed", False),
            "sku": _supplier_sku(oi),
            "files": {"template_tiff": "%s_template.tiff" % uid, "poster_png": "%s_poster.png" % uid},
        },
        "color_specifications": double_for_supplier(
            add_areas(uid, supplier_colors(colors), oi["width_cm"], oi["height_cm"], colors)),
    }
    with open(os.path.join(d, "%s_supplier.json" % uid), "w", encoding="utf-8") as f:
        json.dump(supplier_json, f, ensure_ascii=False, indent=2)
    write_supplier_text(uid, supplier_json)

    # Nettoyage : un seul JSON, pas de doublons, pas d'intermediaires.
    for junk in (f"{uid}_palette.png", "order.json",
                 f"{uid}_poster.svg", f"{uid}_poster.tiff", f"{uid}_preview.tiff"):
        pth = os.path.join(d, junk)
        if os.path.exists(pth):
            os.remove(pth)

    return {"dir": d, "files": [f"{uid}_template.svg", f"{uid}_poster.png", f"{uid}_supplier.json", f"{uid}_supplier.txt",
                                f"{uid}_supplier_colors.jpg"]}
