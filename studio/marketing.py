"""Marketing Corner : a partir d'une image, genere des promos HTML autoportantes
+ un GIF de concatenation des frames cles (photo -> toile coloriee -> toile numerotee)."""
import os, base64, glob
from django.conf import settings


def _b64(path):
    if not path or not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lstrip(".").lower() or "png"
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    with open(path, "rb") as f:
        return "data:image/%s;base64,%s" % (mime, base64.b64encode(f.read()).decode())


def _assets(uid):
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    src = None
    for p in glob.glob(os.path.join(d, "%s_source_*" % uid)) + [os.path.join(d, "source.jpg")]:
        if os.path.exists(p):
            src = p; break
    logo = os.path.join(settings.BASE_DIR, "studio", "static", "studio", "img", "logo.png")
    return {
        "source": _b64(src),
        "preview": _b64(os.path.join(d, "%s_preview.png" % uid)),
        "template": _b64(os.path.join(d, "%s_template.png" % uid)),
        "palette": _b64(os.path.join(d, "%s_palette.png" % uid)),
        "logo": _b64(logo),
    }


# ---------- Promos HTML : reutilise les templates VALIDES + injecte le produit ----------
import re as _re

_TPL_DIR = os.path.join(settings.BASE_DIR, "studio", "marketing_templates")
TEMPLATES = [
    ("slider", "Photo vers toile"),
    ("shiny", "Vide vers plein (shiny)"),
    ("zoom", "Zoom sur la toile"),
]

# Message (gros titre) par defaut, par promo -> editable individuellement
DEFAULT_MSG = {
    "slider": "Votre souvenir devient chef-d'\u0153uvre",
    "shiny": "La magie prend vie, pinceau apr\u00e8s pinceau",
    "zoom": "Chaque d\u00e9tail, une \u00e9motion",
}
DEFAULT_SUB = {
    "slider": "Cr\u00e9\u00e9 \u00e0 partir de VOTRE photo\nUn tableau unique \u00e0 offrir ou garder\nLe plaisir de peindre, sans savoir dessiner",
    "shiny": "De la photo \u00e0 la toile en 1 minute\nPr\u00eat \u00e0 peindre, num\u00e9ro par num\u00e9ro\nAper\u00e7u gratuit",
    "zoom": "Une pr\u00e9cision qui capture l'\u00e9motion\nQualit\u00e9 galerie\nAper\u00e7u gratuit",
}

_ANIM_CSS = ('<style>.magic{animation:mgin .8s cubic-bezier(.2,1.2,.3,1) both}'
             '@keyframes mgin{from{opacity:0;transform:translateY(22px) scale(.96)}to{opacity:1;transform:none}}'
             '.magic .em{display:inline-block;animation:mgpop 2.6s ease-in-out infinite}'
             '@keyframes mgpop{0%,100%{transform:scale(1);filter:none}50%{transform:scale(1.04);filter:brightness(1.12)}}'
             '</style>')


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _headline(html, message):
    """Remplace le gros titre du gabarit (<div class=\"magic\">) par le message (derniers mots en accent)."""
    if not message:
        return html
    words = message.split()
    if len(words) >= 3:
        head = _esc(" ".join(words[:-2])); em = _esc(" ".join(words[-2:]))
        inner = '%s<br><span class="em">%s</span>' % (head, em)
    elif len(words) == 2:
        inner = '%s <span class="em">%s</span>' % (_esc(words[0]), _esc(words[1]))
    else:
        inner = '<span class="em">%s</span>' % _esc(message)
    html = _re.sub(r'(<div class="magic">).*?(</div>)', lambda m: m.group(1) + inner + m.group(2),
                   html, count=1, flags=_re.S)
    if "</head>" in html:
        html = html.replace("</head>", _ANIM_CSS + "</head>", 1)
    else:
        html = _ANIM_CSS + html
    return html


def _subtitles(html, text):
    """Remplace les sous-titres (.claim) du gabarit par les lignes fournies (1 par ligne)."""
    if text is None:
        return html
    lines = [l.strip() for l in text.replace("\r", "").split("\n") if l.strip()]
    accents = ["em", "o", "g"]
    new = ""
    for i, l in enumerate(lines):
        words = l.split()
        if len(words) >= 2:
            new += '<div class="claim">%s <span class="%s">%s</span></div>' % (
                _esc(" ".join(words[:-1])), accents[i % 3], _esc(words[-1]))
        else:
            new += '<div class="claim"><span class="%s">%s</span></div>' % (accents[i % 3], _esc(l))
    if '<div class="claim">' in html:
        html = _re.sub(r'<div class="claim">.*</div>(?=\s*<div class="cta">)', new, html, count=1, flags=_re.S)
    elif new and '<div class="cta">' in html:
        html = html.replace('<div class="cta">', new + '<div class="cta">', 1)
    return html



def _read_svg(path):
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def _inject(html, preview_svg, template_svg, source_b64):
    """Remplace les SVG du template par ceux du produit + la photo source."""
    if preview_svg:
        html = _re.sub(r'<svg xmlns="http://www\.w3\.org/2000/svg"[^>]*geometricPrecision[^>]*>.*?</svg>',
                       lambda m: preview_svg, html, flags=_re.S)
    if template_svg:
        html = _re.sub(r'<svg xmlns="http://www\.w3\.org/2000/svg" version="1\.1" width="(?:400|500)\.0mm".*?</svg>',
                       lambda m: template_svg, html, flags=_re.S)
    if source_b64:
        html = _re.sub(r'data:image/jpeg;base64,[A-Za-z0-9+/=]+', lambda m: source_b64, html, count=1)
    return html


def _clickable(link):
    """Rend le CTA du gabarit cliquable vers le lien, sans ajouter de couche visuelle."""
    if not link:
        return ""
    return ("""<script>document.addEventListener('DOMContentLoaded',function(){
var L=%r;var rx=/Testez notre algorithme|Cr\u00e9er la mienne|Cr..er la mienne/i;
document.querySelectorAll('a,button,div,span').forEach(function(el){
 if(el.children.length===0&&rx.test(el.textContent||'')){var t=el.closest('a,button,div')||el;
  t.style.cursor='pointer';t.addEventListener('click',function(e){e.preventDefault();window.open(L,'_blank');});}});
});</script>""" % link)


def _texts(html, cta, tagline, link):
    """Remplace les textes editables du gabarit (mono-couche : on edite le gabarit lui-meme)."""
    import re as _r
    if cta:
        html = html.replace("Testez notre algorithme", cta)
    if tagline:
        html = _r.sub(r"Gratuit , en moins d'une minute", tagline, html)
        html = _r.sub(r", en moins d'une minute", tagline, html)
    if link:
        html = _r.sub(r'href="https?://[^"]*paintit[^"]*"', 'href="%s"' % link, html)
    return html


def build_all(uid, cta="Testez notre algorithme", messages=None, subs=None, brand="PaintIt", link="https://paintit.click"):
    """Genere les promos (templates valides + produit injecte) + le GIF."""
    a = _assets(uid)
    if not a["preview"]:
        raise ValueError("Toile introuvable pour %s (lance la generation d'abord)." % uid)
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    preview_svg = _read_svg(os.path.join(d, "%s_preview.svg" % uid))
    template_svg = _read_svg(os.path.join(d, "%s_template.svg" % uid))
    outdir = os.path.join(settings.MEDIA_ROOT, "marketing", uid)
    os.makedirs(outdir, exist_ok=True)
    messages = messages or {}
    subs = subs or {}
    cta = (cta or "").strip()
    click = _clickable(link)
    produced = []
    for key, label in TEMPLATES:
        tpl = os.path.join(_TPL_DIR, "%s.html" % key)
        if not os.path.exists(tpl):
            continue
        html = open(tpl, encoding="utf-8").read()
        html = _inject(html, preview_svg, template_svg, a["source"])
        html = _headline(html, (messages.get(key) or DEFAULT_MSG.get(key, "")).strip())
        html = _subtitles(html, subs.get(key) if subs.get(key) is not None else DEFAULT_SUB.get(key))
        html = _texts(html, cta, "", link)
        if click and "</body>" in html:
            html = html.replace("</body>", click + "</body>", 1)
        path = os.path.join(outdir, "promo_%s.html" % key)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        produced.append((label, path, "web"))
    # GIF pret-a-poster : 3 formats FB/Insta
    gif_title = (messages.get("gif") or messages.get("slider") or DEFAULT_MSG["slider"]).strip()
    for fmt, (ratio, flabel) in {"square": ((1, 1), "Carre , FB/Insta feed"),
                                 "portrait": ((4, 5), "Portrait , Insta feed"),
                                 "story": ((9, 16), "Story , 9:16")}.items():
        gif = build_gif(uid, os.path.join(outdir, "promo_%s.gif" % fmt),
                        brand=brand or "PaintIt", link=link or "",
                        title=gif_title, message=(messages.get("gif_sub") or cta or ""), ratio=ratio)
        if gif:
            produced.append(("GIF " + flabel, gif, "gif"))
    return produced


def build_gif(uid, out_path, brand="PaintIt", link="", title="", message="", ratio=(4, 5)):
    """GIF de concatenation : photo -> toile coloriee -> toile numerotee, avec fondu + logo/marque."""
    from PIL import Image, ImageDraw, ImageFont
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    paths = []
    for p in glob.glob(os.path.join(d, "%s_source_*" % uid))[:1]:
        paths.append(p)
    paths += [os.path.join(d, "%s_preview.png" % uid), os.path.join(d, "%s_template.png" % uid)]
    imgs = []
    rw, rh = ratio
    if rw >= rh:
        W = 720; H = int(round(W * rh / rw))
    else:
        H = 1080; W = int(round(H * rw / rh))
    for p in paths:
        if os.path.exists(p):
            im = Image.open(p).convert("RGB")
            im = im.resize((W, int(im.height * W / im.width)))
            canvas = Image.new("RGB", (W, H), "white")
            canvas.paste(im, (0, max(0, (H - im.height) // 2)))
            imgs.append(canvas)
    if not imgs:
        return None
    frames = []
    hold = 12          # frames d'arret sur chaque image
    fade = 8           # frames de fondu
    for i, im in enumerate(imgs):
        frames += [im] * hold
        nxt = imgs[(i + 1) % len(imgs)]
        for s in range(1, fade + 1):
            frames.append(Image.blend(im, nxt, s / (fade + 1.0)))
    # Incrustation logo (haut-gauche) + bandeau marque/URL (bas) sur chaque frame
    logo_im = None
    lp = os.path.join(settings.BASE_DIR, "studio", "static", "studio", "img", "logo.png")
    if os.path.exists(lp):
        try:
            logo_im = Image.open(lp).convert("RGBA")
            lw = 150; logo_im = logo_im.resize((lw, max(1, int(logo_im.height * lw / logo_im.width))))
        except Exception:
            logo_im = None
    def _font(sz):
        try:
            return ImageFont.truetype("DejaVuSans-Bold.ttf", int(sz))
        except Exception:
            return ImageFont.load_default()
    caption = link.replace("https://", "").replace("http://", "") if link else ""

    def _wrap(draw, text, fnt, maxw):
        words, lines, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if draw.textbbox((0, 0), t, font=fnt)[2] <= maxw or not cur:
                cur = t
            else:
                lines.append(cur); cur = w
        if cur:
            lines.append(cur)
        return lines

    def _fit(draw, text, maxw, start, min_sz, max_lines):
        sz = start
        while sz > min_sz:
            f = _font(sz); ls = _wrap(draw, text, f, maxw)
            if len(ls) <= max_lines and all(draw.textbbox((0, 0), l, font=f)[2] <= maxw for l in ls):
                return f, ls
            sz -= 3
        f = _font(min_sz); return f, _wrap(draw, text, f, maxw)

    d0 = ImageDraw.Draw(frames[0])
    ftitle, title_lines = _fit(d0, title or "", W - 56, int(W / 9.5), int(W / 20), 3)
    branded = []
    for fr in frames:
        fr = fr.convert("RGBA")
        dr = ImageDraw.Draw(fr)
        # Scrim degrade bas -> le texte s'integre a l'image (style pub)
        scrim_h = int(H * 0.48)
        grad = Image.new("RGBA", (W, scrim_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for y in range(scrim_h):
            gd.line([(0, y), (W, y)], fill=(8, 12, 26, int(235 * (y / scrim_h) ** 1.3)))
        fr.alpha_composite(grad, (0, H - scrim_h))
        # Logo haut-gauche
        if logo_im is not None:
            fr.alpha_composite(logo_im, (22, 22))
        # Gros titre integre, bas
        th = sum(dr.textbbox((0, 0), l, font=ftitle)[3] + 8 for l in title_lines)
        cta_h = int(W / 13) + 26
        yy = H - 40 - cta_h - th
        for l in title_lines:
            bb = dr.textbbox((0, 0), l, font=ftitle)
            x = (W - (bb[2] - bb[0])) / 2
            dr.text((x + 2, yy + 2), l, font=ftitle, fill=(0, 0, 0, 170))   # ombre
            dr.text((x, yy), l, font=ftitle, fill=(255, 255, 255, 255))
            yy += (bb[3] - bb[1]) + 8
        # CTA pill (message ou url)
        cta_txt = (message or caption or "").strip()
        if cta_txt:
            fc = _font(int(W / 20))
            bb = dr.textbbox((0, 0), cta_txt, font=fc)
            pw, ph = (bb[2] - bb[0]) + 44, (bb[3] - bb[1]) + 24
            px, py = (W - pw) / 2, H - 34 - ph
            pill = Image.new("RGBA", (int(pw), int(ph)), (0, 0, 0, 0))
            pd = ImageDraw.Draw(pill)
            pd.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=int(ph / 2), fill=(47, 107, 242, 255))
            fr.alpha_composite(pill, (int(px), int(py)))
            dr.text((px + 22, py + 10), cta_txt, font=fc, fill=(255, 255, 255, 255))
        # URL discrete en bas
        if caption:
            fu = _font(int(W / 34))
            bb = dr.textbbox((0, 0), caption, font=fu)
            dr.text(((W - (bb[2] - bb[0])) / 2, H - 26), caption, font=fu, fill=(210, 220, 240, 255))
        branded.append(fr.convert("P", palette=Image.ADAPTIVE))
    branded[0].save(out_path, save_all=True, append_images=branded[1:], duration=90, loop=0, optimize=True)
    return out_path

