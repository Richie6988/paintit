"""Fichiers d'impression HAUTE QUALITE de la toile numerotee (fournisseur).

Source : <uid>_template.svg (vectoriel, en mm). Aucune dependance systeme (pas de cairo).
  - <uid>_template.pdf  : PDF VECTORIEL a la taille physique exacte (traits + chiffres, police embarquee)
                          -> nettete infinie, format prefere des imprimeurs ;
  - <uid>_template.tiff : raster 600 dpi (plafonne pour les tres grands formats), traits anti-crenelage
                          avec precision sub-pixel, chiffres en vraie typo, niveaux de gris 8 bits, LZW.
Finitions « premium » : traits gris anthracite fins et reguliers, chiffres gris moyen avec un fin
halo blanc (restent lisibles meme quand ils touchent un trait), marges de securite respectees.
"""
import os
import re

LINE_MM = 0.2          # epaisseur des traits (mm) : fin mais bien visible sous la peinture
LINE_GREY = 45         # anthracite (0 = noir)
NUM_GREY = 105         # chiffres gris moyen : lisibles, couverts par la peinture
HALO = 0.14            # halo blanc autour des chiffres (fraction de la taille du chiffre)
MAX_MEGAPIXELS = 160   # plafond memoire du TIFF (dpi reduit automatiquement au-dela)

_FONT_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "static", "studio", "fonts", "print.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]


def _font_path():
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def parse_template(svg):
    """-> (w_mm, h_mm, [(points, closed)], [(x, y_baseline, size, text)])"""
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    w, h = float(m.group(1)), float(m.group(2))
    paths = []
    for d in re.findall(r'<path d="([^"]+)"', svg):
        for sub in re.split(r"(?=M )", d.strip()):
            sub = sub.strip()
            if not sub:
                continue
            closed = sub.endswith("Z")
            nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", sub)]
            pts = list(zip(nums[0::2], nums[1::2]))
            if len(pts) >= 2:
                paths.append((pts, closed))
    texts = [(float(x), float(y), float(fs), t.strip()) for x, y, fs, t in
             re.findall(r'<text x="([\d.]+)" y="([\d.]+)" font-size="([\d.]+)">([^<]+)</text>', svg)]
    return w, h, paths, texts


def render_pdf(svg, out_path):
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    w, h, paths, texts = parse_template(svg)
    font = "Helvetica"
    fp = _font_path()
    if fp:
        try:
            pdfmetrics.registerFont(TTFont("PaintItPrint", fp)); font = "PaintItPrint"
        except Exception:
            pass
    c = pdfcanvas.Canvas(out_path, pagesize=(w * mm, h * mm))
    c.setTitle("PaintIt - toile numerotee"); c.setAuthor("PaintIt"); c.setCreator("PaintIt print engine")
    c.setLineWidth(LINE_MM * mm); c.setLineJoin(1); c.setLineCap(1)
    g = LINE_GREY / 255.0
    c.setStrokeColorRGB(g, g, g)
    for pts, closed in paths:
        p = c.beginPath()
        p.moveTo(pts[0][0] * mm, (h - pts[0][1]) * mm)
        for x, y in pts[1:]:
            p.lineTo(x * mm, (h - y) * mm)
        if closed:
            p.close()
        c.drawPath(p, stroke=1, fill=0)
    ng = NUM_GREY / 255.0
    for x, y, fs, t in texts:
        size = fs * mm
        # 1) halo blanc (contour epais) 2) chiffre plein par-dessus
        for mode, colour in ((1, (1, 1, 1)), (0, (ng, ng, ng))):
            to = c.beginText()
            to.setFont(font, size)
            to.setTextRenderMode(mode)
            c.setStrokeColorRGB(*colour); c.setFillColorRGB(*colour)
            c.setLineWidth(size * HALO * 2)
            tw = c.stringWidth(t, font, size)
            to.setTextOrigin(x * mm - tw / 2, (h - y) * mm)
            to.textOut(t)
            c.drawText(to)
        c.setLineWidth(LINE_MM * mm); c.setStrokeColorRGB(g, g, g)
    c.showPage(); c.save()
    return out_path


def render_tiff(svg, out_path, dpi=600):
    import numpy as np
    import cv2
    from PIL import Image, ImageDraw, ImageFont
    w, h, paths, texts = parse_template(svg)
    # plafond memoire : on reduit le dpi pour les tres grands formats
    mp = (w / 25.4 * dpi) * (h / 25.4 * dpi) / 1e6
    if mp > MAX_MEGAPIXELS:
        dpi = int(dpi * (MAX_MEGAPIXELS / mp) ** 0.5)
    k = dpi / 25.4                                   # px par mm
    W, H = int(round(w * k)), int(round(h * k))
    img = np.full((H, W), 255, np.uint8)
    SH = 4                                           # precision sub-pixel (1/16 px)
    thick = max(1, int(round(LINE_MM * k)))
    for pts, closed in paths:
        arr = (np.array(pts, np.float64) * k * (1 << SH)).round().astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(img, [arr], closed, LINE_GREY, thick, cv2.LINE_AA, SH)
    pil = Image.fromarray(img, "L")
    draw = ImageDraw.Draw(pil)
    fp = _font_path()
    cache = {}
    for x, y, fs, t in texts:
        px = max(6, int(round(fs * k)))
        if px not in cache:
            try:
                cache[px] = ImageFont.truetype(fp, px) if fp else ImageFont.load_default(px)
            except Exception:
                cache[px] = ImageFont.load_default()
        draw.text((x * k, y * k), t, font=cache[px], fill=NUM_GREY, anchor="ms",
                  stroke_width=max(1, int(round(px * HALO))), stroke_fill=255)
    pil.save(out_path, format="TIFF", compression="tiff_lzw", dpi=(dpi, dpi))
    return out_path, dpi, (W, H)


def export(uid, media_root, dpi=600):
    """Genere <uid>_template.pdf + <uid>_template.tiff depuis <uid>_template.svg. -> {nom: chemin}"""
    d = os.path.join(media_root, "orders", uid)
    sp = os.path.join(d, "%s_template.svg" % uid)
    if not os.path.exists(sp):
        return {}
    svg = open(sp, encoding="utf-8").read()
    made = {}
    pdf = os.path.join(d, "%s_template.pdf" % uid)
    render_pdf(svg, pdf + ".tmp"); os.replace(pdf + ".tmp", pdf); made[os.path.basename(pdf)] = pdf
    tif = os.path.join(d, "%s_template.tiff" % uid)
    render_tiff(svg, tif + ".tmp.tiff", dpi); os.replace(tif + ".tmp.tiff", tif); made[os.path.basename(tif)] = tif
    return made
