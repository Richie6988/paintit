"""Videos « satisfying » pour TikTok / Reels / Shorts : une toile du catalogue se remplit toute seule.

Rendu 100 % serveur, sans navigateur : les zones du SVG DigiPaint sont rasterisees une fois en carte d'etiquettes,
puis chaque image de la video est composee en numpy (fondu rapide de chaque zone), envoyee a ffmpeg (H.264 9:16).
  - mode « random » : ordre aleatoire, le rythme accelere vers la fin (courbe ease-in) ;
  - mode « colors » : couleur apres couleur, chaque couleur remplit ses zones en cascade.
Musique : generee par le code (accords doux + arpege qui accelere avec le remplissage), donc libre de droits.
Aucune photo client : seules les toiles du catalogue et du studio creatif sont proposees.
"""
import json
import math
import os
import re
import subprocess
import wave

import numpy as np
from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
INTRO, OUTRO = 0.1, 3.4                       # ca demarre tout de suite ; fin = toile finie + nom qui se colorie
PHOTO_HOLD, PHOTO_FADE = 0.6, 0.25             # avant / apres : photo breve puis fondu vers la toile vierge
TOP, SIDE, CANVAS_H = 330, 24, 1250            # zone accroche en haut, marges laterales, hauteur max de la toile
BG = (247, 244, 238)
INK = (18, 34, 79)
_PATH_RX = re.compile(r'<path\b[^>]*?data-n="(\d+)"[^>]*?d="([^"]+)"', re.S)
_PATH_RX2 = re.compile(r'<path\b[^>]*?d="([^"]+)"[^>]*?data-n="(\d+)"', re.S)
_FONT = os.path.join(settings.BASE_DIR, "studio", "static", "studio", "fonts", "Baloo2-Bold.ttf")


def _font(size):
    try:
        return ImageFont.truetype(_FONT, size)
    except Exception:
        return ImageFont.load_default()


def sources():
    """Toiles utilisables (images importees ici, catalogue, studio creatif), jamais de commande client. -> [(uid, titre)]"""
    from .models import DigitalCanvas, MarketingAd
    out, seen = [], set()
    for c in DigitalCanvas.objects.filter(source="library").order_by("-created_at")[:300]:
        if c.uid not in seen and _svg_path(c.uid):
            seen.add(c.uid); out.append((c.uid, c.title or c.uid))
    root = os.path.join(settings.MEDIA_ROOT, "orders")
    imported = [d for d in os.listdir(root) if d.startswith("vid-")] if os.path.isdir(root) else []
    for uid in sorted(imported, key=lambda d: -os.path.getmtime(os.path.join(root, d))):
        if _svg_path(uid):                               # images importees / densites regenerees depuis cette page
            seen.add(uid); out.insert(0, (uid, _label(uid)))
    for uid, camp in MarketingAd.objects.order_by("-created_at").values_list("image_uid", "campaign")[:300]:
        if uid not in seen and _svg_path(uid):
            seen.add(uid); out.append((uid, "Studio créatif · " + (camp or uid)))
    return out


def _label(uid):
    try:
        with open(os.path.join(settings.MEDIA_ROOT, "orders", uid, "label.txt"), encoding="utf-8") as fh:
            return fh.read().strip()[:120] or "Image importée"
    except OSError:
        return "Image importée"


def save_label(uid, label):
    with open(os.path.join(settings.MEDIA_ROOT, "orders", uid, "label.txt"), "w", encoding="utf-8") as fh:
        fh.write(label)


def _svg_path(uid):
    p = os.path.join(settings.MEDIA_ROOT, "orders", uid, "%s_digipaint.svg" % uid)
    return p if os.path.exists(p) else None


def _palette(uid):
    p = os.path.join(settings.MEDIA_ROOT, "orders", uid, "%s_colors.json" % uid)
    with open(p, encoding="utf-8") as fh:
        return {int(c["number"]): tuple(c["rgb"]) for c in json.load(fh)}


def load_zones(uid, box_w, box_h):
    """-> (labels HxW int32 [-1 = fond], zone_colors (N,3) uint8, zone_numbers (N,), area (N,), outline mask)"""
    with open(_svg_path(uid), encoding="utf-8") as fh:
        svg = fh.read()
    vb = re.search(r'viewBox="([\d.\s-]+)"', svg)
    vx, vy, vw, vh = [float(x) for x in vb.group(1).split()]
    scale = min(box_w / vw, box_h / vh)
    cw, ch = int(round(vw * scale)), int(round(vh * scale))
    zones = [(int(n), d) for n, d in _PATH_RX.findall(svg)] or [(int(n), d) for d, n in _PATH_RX2.findall(svg)]
    pal = _palette(uid)
    polys = []
    for n, d in zones:
        subs = []
        for part in re.split(r"(?=M)", d.strip()):
            pts = re.findall(r"(-?[\d.]+)[ ,]+(-?[\d.]+)", part)
            if len(pts) >= 3:
                subs.append([((float(x) - vx) * scale, (float(y) - vy) * scale) for x, y in pts])
        if subs:
            area = abs(_area(subs[0]))
            polys.append((area, n, subs))
    # rasterisation exacte « evenodd » : chaque sous-chemin bascule l'appartenance (morceaux disjoints et trous)
    labels = np.full((ch, cw), -1, np.int32)
    numbers, colors, areas = [], [], []
    for i, (area, n, subs) in enumerate(polys):
        xs = [x for sp in subs for x, _y in sp]; ys = [y for sp in subs for _x, y in sp]
        x0, y0 = max(0, int(min(xs)) - 1), max(0, int(min(ys)) - 1)
        x1, y1 = min(cw, int(max(xs)) + 2), min(ch, int(max(ys)) + 2)
        if x1 <= x0 or y1 <= y0:
            continue
        acc = np.zeros((y1 - y0, x1 - x0), bool)
        for sp in subs:
            m = Image.new("1", (x1 - x0, y1 - y0), 0)
            ImageDraw.Draw(m).polygon([(x - x0, y - y0) for x, y in sp], fill=1)
            acc ^= np.array(m, bool)
        labels[y0:y1, x0:x1][acc] = len(numbers)
        numbers.append(n); colors.append(pal.get(n, (200, 200, 200))); areas.append(area)
    # contours fins entre zones (sans numeros)
    edge = np.zeros(labels.shape, bool)
    edge[1:, :] |= labels[1:, :] != labels[:-1, :]
    edge[:, 1:] |= labels[:, 1:] != labels[:, :-1]
    return labels, np.array(colors, np.uint8), np.array(numbers), np.array(areas), edge


def _area(pts):
    return 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))


def schedule(n_zones, numbers, mode, fill_seconds, seed=0, areas=None, centers=None):
    """Instant de debut de remplissage de chaque zone (secondes, a partir de la fin de l'intro).
    Rythme : c(u) = N (0.3 u + 0.7 u^3) -> ca demarre tout de suite (part lineaire) et accelere sans cesse jusqu'a
    la fin (part cubique), en phase avec le tempo de la musique qui accelere aussi.
    Modes : flow (vague qui se propage depuis un point : continuite, l'oeil suit), random, colors."""
    rng = np.random.default_rng(seed)
    u = np.linspace(0, 1, 2001)
    frac = 0.3 * u + 0.7 * u ** 3
    when = lambda rank: fill_seconds * np.interp(rank / max(1, n_zones), frac, u)
    start = np.zeros(n_zones)
    if mode == "colors":
        order_cols = rng.permutation(np.unique(numbers)).tolist()
        rank = 0
        for c in order_cols:
            for z in rng.permutation(np.where(numbers == c)[0]):
                start[z] = when(rank); rank += 1
        return start
    if mode == "flow" and centers is not None and len(centers):
        c = np.asarray(centers, float)
        lo, hi = c.min(0), c.max(0)
        seeds = [(0.5, 1.0), (0.5, 0.5), (0.0, 1.0), (1.0, 1.0), (0.5, 0.0)]     # bas, centre, coins bas, haut
        sx, sy = seeds[int(rng.integers(len(seeds)))]
        p = lo + (hi - lo) * np.array([sx, sy])
        d = np.hypot(*(c - p).T)
        d = d / max(1e-6, d.max()) + rng.normal(0, 0.06, n_zones)            # front de vague legerement organique
        for rank, z in enumerate(np.argsort(d)):
            start[z] = when(rank)
        return start
    # aleatoire, mais les grandes zones sortent plutot en premier : la toile prend vie des la 1re seconde
    w = np.ones(n_zones) if areas is None else np.maximum(np.asarray(areas, float), 1e-6) ** 0.35
    order = np.argsort(-rng.random(n_zones) ** (1.0 / w))
    for rank, z in enumerate(order):
        start[z] = when(rank)
    return start


# ---------------- Musique generee (libre de droits) ----------------
DENSITIES = [("orig", "Celle de la toile"), ("easy", "Facile (grandes zones)"), ("medium", "Moyen"),
             ("expert", "Expert (zones fines)")]
MUSIC_STYLES = [("ambient", "Douce"), ("electro", "Électro"), ("space", "Space"), ("none", "Aucune")]
_hz = lambda m: 440.0 * 2 ** ((m - 69) / 12)


def _add(out, sr, at, sig):
    a = int(at * sr)
    if a >= len(out):
        return
    sig = sig[:len(out) - a]
    out[a:a + len(sig)] += sig


def _tone(sr, dur, f, amp, decay, harm=(1.0,), detune=0.0):
    tt = np.arange(int(dur * sr)) / sr
    s = sum(h * np.sin(2 * np.pi * f * (k + 1) * tt) for k, h in enumerate(harm) if h)
    if detune:
        s = s + np.sin(2 * np.pi * f * (1 + detune) * tt)
    return amp * np.exp(-tt * decay) * s


def _ding(out, sr, at):
    for m in (72, 76, 79, 84):
        _add(out, sr, at, _tone(sr, 2.5, _hz(m), 0.06, 2.2))


def _ambient(out, sr, duration, fill_end):
    prog = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]   # Am F C G
    bar = 4.0
    t = np.arange(int((bar + 1) * sr)) / sr
    for i, st in enumerate(np.arange(0, duration, bar)):
        env = np.minimum(1, t / 0.8) * np.exp(-np.maximum(0, t - bar) * 3)
        for m in prog[i % 4]:
            _add(out, sr, st, 0.05 * env * (np.sin(2 * np.pi * _hz(m) * t) + 0.3 * np.sin(4 * np.pi * _hz(m) * t)))
    pos, step = 0.3, 0
    while pos < duration - 0.3:
        m = prog[int(pos // bar) % 4][step % 3] + 12 * (1 + (step // 3) % 2)
        _add(out, sr, pos, _tone(sr, 0.6, _hz(m), 0.07, 6))
        f = min(1.0, pos / max(1e-3, fill_end))
        pos += 0.42 - 0.3 * f ** 2 if pos < fill_end else 0.5
        step += 1


def _electro(out, sr, duration, fill_end, rng):
    prog = [[45, 57, 60, 64], [41, 53, 57, 60], [48, 55, 60, 64], [43, 55, 59, 62]]   # Am F C G (basse + accord)
    beat = 60 / 124.0
    tk = np.arange(int(0.35 * sr)) / sr
    kick = 0.32 * np.sin(2 * np.pi * (45 * tk + 90 / 18 * (1 - np.exp(-18 * tk)))) * np.exp(-tk * 9)
    nz = rng.standard_normal(int(0.06 * sr)); hat = 0.05 * np.diff(nz, prepend=0) * np.exp(-np.arange(len(nz)) / sr * 60)
    saw = [1, .5, .33, .25, .2, .16]
    i, pos = 0, 0.0
    while pos < min(duration, fill_end + 0.05):
        bar = int(i // 4)
        ch = prog[bar % 4]
        f = pos / max(1e-3, fill_end)
        _add(out, sr, pos, kick)
        if f > 0.12:
            _add(out, sr, pos + beat / 2, hat)
        for e in (0, 0.5):                                          # basse en croches
            _add(out, sr, pos + e * beat, _tone(sr, beat * 0.45, _hz(ch[0]), 0.05, 9, saw))
        if f > 0.35:                                                # arpege en doubles croches
            for q in range(4):
                m = ch[1 + (i * 4 + q) % 3] + 12
                _add(out, sr, pos + q * beat / 4, _tone(sr, beat / 3, _hz(m), 0.03 + 0.02 * f, 14, (1, 0, .33, 0, .2)))
        if i % 4 == 0:                                              # nappe
            for m in ch[1:]:
                _add(out, sr, pos, _tone(sr, beat * 4, _hz(m), 0.018, 0.6, (1, .4), 0.004))
        i += 1; pos += beat
    for m in prog[0][1:]:                                           # apres le « drop » : nappe seule
        _add(out, sr, fill_end, _tone(sr, max(0.5, duration - fill_end), _hz(m), 0.03, 0.5, (1, .4), 0.004))


def _space(out, sr, duration, fill_end, rng):
    prog = [[50, 57, 62, 64], [46, 53, 58, 62], [41, 48, 53, 57], [48, 55, 60, 62]]   # Dm9 Bb F Cadd9
    bar = 6.0
    for i, st in enumerate(np.arange(-1, duration, bar)):
        tt = np.arange(int((bar + 2.5) * sr)) / sr
        env = np.minimum(1, tt / 2.0) * np.exp(-np.maximum(0, tt - bar) * 1.2) * (0.8 + 0.2 * np.sin(2 * np.pi * 0.25 * tt))
        for k, m in enumerate(prog[i % 4]):
            f = _hz(m)
            s = np.sin(2 * np.pi * f * tt) + np.sin(2 * np.pi * f * 1.004 * tt) + 0.25 * np.sin(2 * np.pi * f * 2.002 * tt)
            _add(out, sr, max(0, st), (0.035 if k else 0.05) * env * s)
    penta = [74, 76, 79, 81, 83, 86, 88, 91]
    pos = 0.4
    while pos < fill_end:                                           # etoiles : de plus en plus denses
        f = pos / max(1e-3, fill_end)
        _add(out, sr, pos, _tone(sr, 1.6, _hz(int(rng.choice(penta))), 0.045, 3.5, (1, 0, .15)))
        pos += max(0.11, 0.9 - 0.8 * f ** 1.5) * rng.uniform(0.7, 1.3)
    d = int(0.38 * sr)                                              # echo spatial
    for _ in range(3):
        out[d:] += 0.42 * out[:-d]


PENTA = [60, 62, 64, 67, 69, 72, 74, 76, 79, 81, 84]          # gamme pentatonique (do) : jamais de fausse note


def _plucks(out, sr, plucks):
    """Sonification : une note douce par zone qui se remplit (aigu = couleur claire). Les remplissages qui
    s'accelerent deviennent une cascade musicale ; notes trop rapprochees fusionnees (pas de bouillie)."""
    last = -1.0
    for t, idx, amp in sorted(plucks):
        if t - last < 0.045:
            continue
        last = t
        f = _hz(PENTA[max(0, min(len(PENTA) - 1, idx))])
        sig = _tone(sr, 0.45, f, 0.075 * amp, 8.5, (1, 0.32, 0.08))
        sig[:int(0.004 * sr)] *= np.linspace(0, 1, int(0.004 * sr))                 # attaque douce : pas de clic
        _add(out, sr, t, sig)


def music(path, duration, fill_end, seed=0, sr=44100, style="ambient", plucks=None):
    """Musique generee par le code (donc libre de droits), synchronisee sur le remplissage + « ding » final.
    style « none » + plucks : uniquement les sons de remplissage."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(duration * sr))
    if style == "electro":
        _electro(out, sr, duration, fill_end, rng)
    elif style == "space":
        _space(out, sr, duration, fill_end, rng)
    elif style != "none":
        _ambient(out, sr, duration, fill_end)
    if plucks:
        if style != "none":
            out *= 0.6 / max(1e-6, np.abs(out).max())          # la musique passe derriere les sons de peinture
        _plucks(out, sr, plucks)
    _ding(out, sr, fill_end)
    out *= 0.9 / max(1e-6, np.abs(out).max())
    fade = min(len(out), int(1.2 * sr))
    out[-fade:] *= np.linspace(1, 0, fade)
    pcm = (out * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm.tobytes())
    return path


def _clean(text):
    """La police n'a pas d'emoji : on les retire de l'image (a mettre plutot dans la legende TikTok)."""
    return " ".join("".join(ch for ch in (text or "") if ord(ch) < 0x2190).split())


def _text_layer(hook):
    """Accroche facon TikTok : texte noir sur bulles blanches arrondies, une par ligne (RGBA pre-calcule)."""
    hook = _clean(hook)
    top = Image.new("RGBA", (W, TOP), (0, 0, 0, 0))
    d = ImageDraw.Draw(top)
    if hook:
        f = _font(56)
        lines = _wrap(d, hook, f, W - 200)[:3]
        lh = 78
        y = TOP - 24 - len(lines) * lh                          # colle au haut de la toile, sous la zone UI TikTok
        for l in lines:
            x0, y0, x1, y1 = d.textbbox((0, 0), l, font=f)
            bw = x1 - x0
            px = (W - bw) / 2
            d.rounded_rectangle([px - 22, y, px + bw + 22, y + lh - 6], radius=18, fill=(255, 255, 255, 250))
            d.text((px - x0, y + (lh - 6 - (y1 - y0)) / 2 - y0), l, font=f, fill=(10, 10, 10))
            y += lh
    return np.array(top)


BRAND = "paintit.click"


def _brand_mask():
    """Marque discrete sous la toile : masque alpha (0..1)."""
    f = _font(46)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    l, t, r, b = probe.textbbox((0, 0), BRAND, font=f)
    m = Image.new("L", (r - l + 4, b - t + 4), 0)
    ImageDraw.Draw(m).text((2 - l, 2 - t), BRAND, font=f, fill=255)
    return np.array(m, np.float32)[..., None] / 255.0


def _rainbow(x, lo=160, amp=95):
    """Degrade irise : x quelconque -> RGB 0..255 (par defaut teintes nacrees pastel)."""
    x = np.asarray(x, np.float32)[..., None]
    ph = np.array([0.0, 2.1, 4.2], np.float32)
    return lo + amp * (0.5 + 0.5 * np.cos(2 * np.pi * x + ph))


def _star(size=56):
    """Eclat en etoile (RGBA float) pour les scintillements de fin."""
    yy, xx = np.mgrid[-1:1:size * 1j, -1:1:size * 1j]
    a = np.clip(1 - np.abs(xx) * 9, 0, 1) * np.clip(1 - np.abs(yy), 0, 1) \
        + np.clip(1 - np.abs(yy) * 9, 0, 1) * np.clip(1 - np.abs(xx), 0, 1) \
        + np.exp(-(xx ** 2 + yy ** 2) * 30)
    return np.clip(a, 0, 1).astype(np.float32)[..., None]


def _counter(txt):
    """Pastille sombre (compteur de zones, etiquette « Photo ») : code « satisfying » TikTok."""
    f = _font(38)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    l, t, r, b = probe.textbbox((0, 0), txt, font=f)
    im = Image.new("RGBA", (r - l + 40, b - t + 22), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=im.height // 2, fill=(10, 10, 10, 150))
    d.text((20 - l, 11 - t), txt, font=f, fill=(255, 255, 255))
    return np.array(im)


def _wrap(d, text, f, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textbbox((0, 0), t, font=f)[2] <= maxw or not cur:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines


def _blend(frame, layer, x, y, k=1.0):
    h, w = layer.shape[:2]
    a = layer[..., 3:4].astype(np.float32) * (k / 255.0)
    roi = frame[y:y + h, x:x + w].astype(np.float32)
    frame[y:y + h, x:x + w] = (roi * (1 - a) + layer[..., :3] * a).astype(np.uint8)


def _photo(uid, cw, ch):
    """Photo d'origine recadree (cover) au format de la toile, ou None si absente."""
    from .pipeline import source_file
    p = source_file(os.path.join(settings.MEDIA_ROOT, "orders", uid), uid)
    if not p:
        return None
    try:
        from PIL import ImageOps
        im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
        return np.array(ImageOps.fit(im, (cw, ch), Image.LANCZOS))
    except Exception:
        return None


def hook_text(hook, n_zones, n_colors):
    """Remplace {zones} et {colors} par les vrais chiffres de la toile."""
    return (hook or "").replace("{zones}", str(n_zones)).replace("{colors}", str(n_colors))


SUSPENSE = 0.9          # derniere zone : elle clignote un instant avant d'etre peinte (attente -> recompense)
LOOP = 0.5              # fondu final vers la 1re image : la video boucle sans couture (revisionnage)
ZOOM = 0.07             # lent zoom avant pendant le remplissage, recul a la revelation


_NUM_RX = re.compile(r'<text class="zn" x="([\d.]+)" y="([\d.]+)" font-size="([\d.]+)">(\d+)</text>')
_NUM_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              os.path.join(settings.BASE_DIR, "studio", "static", "studio", "fonts", "Poppins-Bold.ttf")]


def _number_layer(uid, cw, ch, min_px=9):
    """Numeros de la toile (comme sur le kit imprime) -> (indices pixels, alpha). Numeros illisibles (< min_px) omis."""
    with open(_svg_path(uid), encoding="utf-8") as fh:
        svg = fh.read()
    vx, vy, vw, vh = [float(x) for x in re.search(r'viewBox="([\d.\s-]+)"', svg).group(1).split()]
    scale = cw / vw
    img = Image.new("L", (cw, ch), 0)
    d = ImageDraw.Draw(img)
    path = next((f for f in _NUM_FONTS if os.path.exists(f)), None)
    fonts = {}
    for x, y, fs, num in _NUM_RX.findall(svg):
        size = int(round(float(fs) * scale))
        if size < min_px:
            continue
        if size not in fonts:
            fonts[size] = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        d.text(((float(x) - vx) * scale, (float(y) - vy) * scale), num, font=fonts[size], fill=255, anchor="ms")
    a = np.asarray(img, np.float32).ravel() / 255.0
    idx = np.nonzero(a > 0.03)[0]
    return idx, a[idx]


def _smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def render(uid, out_path, mode="flow", duration=65.0, hook="", end_title="", end_sub="", with_music=True,
           seed=0, progress=None, music_style="ambient", before_after=False, fill_sounds=True, show_numbers=True):
    """Genere la video MP4. -> chemin. `progress(f)` recoit 0..1.
    Leviers « satisfying » : peinture qui s'etale depuis le centre de chaque zone (reflet humide + « pop » a la fin),
    barre de progression (envie de voir 100 %), sons de remplissage, derniere zone gardee pour la fin,
    zoom lent, fin irisee puis fondu vers la premiere image (boucle)."""
    import cv2
    import imageio_ffmpeg
    from PIL import ImageFilter
    labels, colors, numbers, areas, edge = load_zones(uid, W - 2 * SIDE, CANVAS_H)
    n = len(colors)
    ch, cw = labels.shape
    ox, oy = (W - cw) // 2, TOP + (CANVAS_H - ch) // 2
    photo = _photo(uid, cw, ch) if before_after else None        # avant / apres : la photo d'origine d'abord
    intro = INTRO + (PHOTO_HOLD + PHOTO_FADE if photo is not None else 0)

    # --- geometrie des zones : surface reelle, centre, distance normalisee de chaque pixel au centre
    lab = np.where(labels < 0, n, labels).astype(np.int32)
    flat = lab.ravel()
    px = np.bincount(flat, minlength=n + 1)[:n].astype(np.float64)
    yy, xx = np.mgrid[0:ch, 0:cw]
    cx = np.bincount(flat, xx.ravel(), n + 1)[:n] / np.maximum(px, 1)
    cy = np.bincount(flat, yy.ravel(), n + 1)[:n] / np.maximum(px, 1)
    dist = np.hypot(xx.ravel() - np.append(cx, 0)[flat], yy.ravel() - np.append(cy, 0)[flat]).astype(np.float32)
    dmax = np.zeros(n + 1, np.float32)
    np.maximum.at(dmax, flat, dist)
    dnorm = dist / np.maximum(dmax[flat], 1)
    order_px = np.argsort(flat, kind="stable")
    bounds = np.searchsorted(flat[order_px], np.arange(n + 2))
    zone_px = [order_px[bounds[z]:bounds[z + 1]] for z in range(n)]
    zone_d = [dnorm[i][:, None] for i in zone_px]

    # --- calendrier : chaque zone s'etale en 0.16-0.65 s selon sa taille ; la derniere est gardee pour la fin
    fill_seconds = max(10.0, duration - intro - OUTRO - SUSPENSE)
    start = schedule(n, numbers, mode, fill_seconds, seed, px, np.c_[cx, cy]) + intro
    dur = np.clip(0.16 + 0.0011 * np.sqrt(px), 0.16, 0.65)
    # derniere zone : bien visible mais pas immense (1 a 3 % de la toile), proche du centre, parmi les dernieres
    late = np.argsort(start)[-max(3, n // 6):]
    share = px[late] / max(px.sum(), 1)
    centr = np.hypot(cx[late] / cw - 0.5, cy[late] / ch - 0.5)
    last = int(late[np.argmin(np.abs(share - 0.02) * 30 + centr)])
    others_end = float(np.max(np.delete(start + dur, last))) if n > 1 else float(start[last])
    start[last] = others_end + SUSPENSE
    fill_end = float(start[last] + dur[last])

    top = _text_layer(hook_text(hook, n, len(np.unique(numbers))))
    brand = _brand_mask()
    bar_y = oy + ch + 12
    bx, by = (W - brand.shape[1]) // 2, oy + ch + 36
    # fin « win » : reflet irise qui balaie la toile + scintillements + marque irisee
    diag = (xx + yy * 0.7).astype(np.float32) / (cw + ch * 0.7)
    iri = _rainbow(diag * 1.6)
    rng = np.random.default_rng(seed + 7)
    star = _star()
    sparks = [(int(rng.uniform(0, cw - 56)), int(rng.uniform(0, ch - 56)), rng.uniform(0.25, 2.6)) for _ in range(16)]
    bm_x = np.linspace(0, 1, brand.shape[1], dtype=np.float32)[None, :]
    col = colors.astype(np.float32)
    edge_idx = np.nonzero(edge.ravel())[0]
    edge_lab = flat[edge_idx]
    edge_rgb = np.array([190, 196, 210], np.uint8)
    last_edges = edge_idx[edge_lab == last]
    if show_numbers:                                   # numeros du kit : ils s'effacent sous la peinture
        num_idx, num_a = _number_layer(uid, cw, ch)
        num_lab = flat[num_idx]
        num_ink = np.array([96, 104, 122], np.float32)
    base = np.zeros((H, W, 3), np.uint8); base[:] = BG
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle([ox - 4, oy + 8, ox + cw + 4, oy + ch + 18], radius=14, fill=60)
    sh = np.array(sh.filter(ImageFilter.GaussianBlur(16)), np.float32)[..., None] / 255.0
    base = (base * (1 - sh * 0.5)).astype(np.uint8)
    _blend(base, top, 0, 0)
    base[bar_y:bar_y + 8, ox:ox + cw] = (226, 230, 239)                     # rail de la barre de progression
    roi = base[by:by + brand.shape[0], bx:bx + brand.shape[1]].astype(np.float32)
    base[by:by + brand.shape[0], bx:bx + brand.shape[1]] = (roi * (1 - brand * 0.85) + np.array(INK) * brand * 0.85).astype(np.uint8)
    pills, photo_tag = {}, _counter("Photo")
    total_frames = int(duration * FPS)

    # --- son : musique choisie + une note par zone (aigu = couleur claire), accord final a la derniere zone
    tmp_audio = out_path + ".wav"
    style = music_style if with_music else "none"
    plucks = None
    if fill_sounds:
        lum = col @ np.array([0.3, 0.59, 0.11], np.float32)
        idx = np.round(lum / 255 * (len(PENTA) - 1)).astype(int)
        amp = 0.45 + 0.55 * np.sqrt(px / max(px.max(), 1))
        plucks = [(float(start[z]), int(idx[z]), float(amp[z])) for z in range(n) if z != last]
    has_audio = style != "none" or bool(plucks)
    if has_audio:
        music(tmp_audio, duration, fill_end, seed, style=style, plucks=plucks)
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", "-"]
    if has_audio:
        cmd += ["-i", tmp_audio, "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    accent = np.array([47, 107, 242], np.float32)
    first = None
    try:
        pal = np.full((n + 1, 3), 255, np.float32)
        for fi in range(total_frames):
            t = fi / FPS
            a = np.clip((t - start) / dur, 0, 1)
            ae = a * a * (3 - 2 * a)
            since = t - (start + dur)
            flash = np.where(since > 0, np.exp(-np.maximum(since, 0) / 0.2), 0.0) * 0.45     # « pop » a la fin de la zone
            done_z = ae >= 1
            pal[:n] = np.where(done_z[:, None], col + (255 - col) * flash[:, None], 255)
            canvas = pal.astype(np.uint8)[lab]
            cflat = canvas.reshape(-1, 3)
            for z in np.nonzero((ae > 0) & (ae < 1))[0]:                   # peinture qui s'etale depuis le centre
                m = np.clip((ae[z] * 1.08 - zone_d[z]) / 0.06, 0, 1)       # front de peinture net
                v = 255 + (col[z] - 255) * m
                v += (255 - v) * (m * (1 - m) * 2.0)                       # reflet humide sur le front de peinture
                cflat[zone_px[z]] = v.astype(np.uint8)
            if t < fill_end:
                cflat[edge_idx[np.append(ae, 1.0)[edge_lab] < 1]] = edge_rgb  # contours seulement autour du non-peint
            if others_end < t < start[last]:                                 # suspense : la derniere zone clignote
                k = 0.5 + 0.5 * math.sin(2 * math.pi * 2.4 * (t - others_end) - math.pi / 2)
                cflat[zone_px[last]] = (255 - (255 - np.array([205, 220, 255], np.float32)) * k).astype(np.uint8)
                cflat[last_edges] = accent.astype(np.uint8)
            if show_numbers and t < fill_end:
                kn = np.clip(1 - np.append(ae, 1.0)[num_lab] * 1.6, 0, 1)
                wn = (num_a * kn)[:, None]
                cflat[num_idx] = (cflat[num_idx] * (1 - wn) + num_ink * wn).astype(np.uint8)
            if photo is not None and t < PHOTO_HOLD + PHOTO_FADE:
                kp = _smooth((t - PHOTO_HOLD) / PHOTO_FADE)
                canvas = (photo * (1 - kp) + canvas * kp).astype(np.uint8)
            # zoom lent : avant pendant le remplissage (vers la derniere zone pendant le suspense), recul a la fin
            if t <= fill_end:
                z = 1 + ZOOM * _smooth((t - intro) / max(1e-3, fill_end - intro))
            else:
                z = 1 + ZOOM * (1 - _smooth((t - fill_end) / 0.9))
            if z > 1.002:
                kf = _smooth((t - others_end) / SUSPENSE) if t > others_end else 0.0
                fx = cw / 2 + (cx[last] - cw / 2) * 0.6 * kf
                fy = ch / 2 + (cy[last] - ch / 2) * 0.6 * kf
                w2, h2 = cw / z, ch / z
                x0 = min(max(fx - w2 / 2, 0), cw - w2); y0 = min(max(fy - h2 / 2, 0), ch - h2)
                M = np.float32([[z, 0, -x0 * z], [0, z, -y0 * z]])
                canvas = cv2.warpAffine(canvas, M, (cw, ch), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            frame = base.copy()
            frame[oy:oy + ch, ox:ox + cw] = canvas
            # progression : compteur de zones peintes (bouge a chaque zone, plus vivant qu'un %) + barre
            nz = int(done_z.sum())
            wbar = int(round(cw * nz / max(n, 1)))
            if wbar:
                frame[bar_y:bar_y + 8, ox:ox + wbar] = (47, 107, 242) if nz < n else (31, 157, 85)
            if nz not in pills:
                pills[nz] = _counter("%d / %d zones" % (nz, n))
            cn = photo_tag if photo is not None and t < PHOTO_HOLD else pills[nz]
            _blend(frame, cn, ox + 18, oy + ch - cn.shape[0] - 18)
            te = t - fill_end
            if te > 0:
                cvf = frame[oy:oy + ch, ox:ox + cw].astype(np.float32)
                kk = np.zeros((ch, cw), np.float32)
                for t0, amp_ in ((0.0, 0.85), (1.3, 0.45)):                   # deux balayages irises
                    c = (te - t0) / 1.1 * 1.6 - 0.3
                    if -0.4 < c < 1.4:
                        kk += amp_ * np.exp(-((diag - c) / 0.09) ** 2)
                kk += 0.10 * min(1.0, te / 0.6)
                kk = np.minimum(kk, 1)[..., None]
                cvf = 255 - (255 - cvf) * (1 - kk * iri / 255)
                for sx, sy, s0 in sparks:                                     # scintillements
                    v = te - s0
                    if 0 < v < 0.7:
                        reg = cvf[sy:sy + 56, sx:sx + 56]
                        reg += (255 - reg) * star * math.sin(math.pi * v / 0.7)
                frame[oy:oy + ch, ox:ox + cw] = np.clip(cvf, 0, 255).astype(np.uint8)
                kb = min(1.0, te / 0.4)
                bcol = _rainbow(bm_x * 1.2 - te * 0.6, 20, 190)
                roi = frame[by:by + brand.shape[0], bx:bx + brand.shape[1]].astype(np.float32)
                ab = brand * kb
                frame[by:by + brand.shape[0], bx:bx + brand.shape[1]] = (roi * (1 - ab) + bcol * ab).astype(np.uint8)
            if first is None:
                first = frame.copy()
            elif t > duration - LOOP:                                        # boucle : retour a la 1re image
                kl = _smooth((t - (duration - LOOP)) / LOOP)
                frame = (frame * (1 - kl) + first * kl).astype(np.uint8)
            proc.stdin.write(frame.tobytes())
            if progress and fi % 30 == 0:
                progress(fi / total_frames)
        proc.stdin.close()
        proc.wait(timeout=900)
    finally:
        if os.path.exists(tmp_audio):
            os.remove(tmp_audio)
    if proc.returncode:
        raise RuntimeError("ffmpeg a échoué (code %s)" % proc.returncode)
    return out_path


def thumbnail(uid, out_path):
    """Image d'apercu (toile finie) pour la liste."""
    labels, colors, numbers, areas, edge = load_zones(uid, 360, 450)
    img = colors[np.where(labels < 0, 0, labels)].copy()
    img[labels < 0] = 255
    Image.fromarray(img.astype(np.uint8)).save(out_path, "JPEG", quality=82)
    return out_path


CAPTIONS = {
    "fr": "Attends la fin 😮‍💨 Une photo devient une peinture par numéros, zone par zone. Combien de temps tu mettrais ? Dis-le en commentaire 👇",
    "en": "Wait for the end 😮‍💨 A photo turned into a paint by numbers, zone by zone. How long would it take you? Tell me in the comments 👇",
}
# Titres facon TikTok : ton perso, minuscules, pas de discours commercial (le lien reste dans la bio)
TITLES = {
    "en": [
        "wait for it…", "the last zone 🤌", "why is this so satisfying", "pov: you can't stop watching",
        "me pretending i have this much patience", "it started as a photo", "rate the final result 1-10",
        "the ending though 😮‍💨", "couldn't stop watching this", "that last color…", "this is my therapy now",
        "watch till the end", "i'd never finish this irl", "no thoughts just colors", "how long would it take you? be honest",
        "should i do another one?", "zone by zone 🎨", "the glow at the end ✨", "guess the picture before it's done",
        "it takes patience", "the before & after 👀", "calmest minute of your day",
    ],
    "fr": [
        "attends la fin…", "la dernière zone 🤌", "pourquoi c'est aussi satisfaisant", "pov : tu peux plus décrocher",
        "c'était juste une photo", "note le résultat de 1 à 10", "moi qui fais semblant d'avoir cette patience",
        "ma thérapie maintenant", "regarde jusqu'à la fin", "zone par zone 🎨", "il faut de la patience",
        "le avant / après 👀", "tu mettrais combien de temps ? sois honnête", "j'en refais une ?",
    ],
}
HASHTAGS = "#satisfying #paintbynumbers #oddlysatisfying #asmr #fyp"


# ---------------- File de rendu (un rendu a la fois : c'est gourmand en CPU) ----------------
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tiktok")
STALE_MINUTES = 20


def _job(pk):
    from django.db import close_old_connections
    from .models import SocialVideo
    close_old_connections()
    v = SocialVideo.objects.filter(pk=pk).first()
    if not v:
        return
    try:
        os.makedirs(os.path.dirname(v.path()), exist_ok=True)
        SocialVideo.objects.filter(pk=pk).update(status="running", progress=0)
        last = [0.0]

        def prog(f):
            if f - last[0] >= 0.02:
                last[0] = f
                SocialVideo.objects.filter(pk=pk).update(progress=round(f, 3))
        tmp = v.path() + ".part.mp4"
        render(v.canvas_uid, tmp, mode=v.mode, duration=v.duration, hook=v.hook, with_music=v.music,
               music_style=v.music_style, seed=v.seed, progress=prog, before_after=v.before_after,
               fill_sounds=v.fill_sounds, show_numbers=v.show_numbers)
        os.replace(tmp, v.path())
        thumbnail(v.canvas_uid, v.path("jpg"))
        SocialVideo.objects.filter(pk=pk).update(status="done", progress=1, error="")
    except Exception as e:                       # pragma: no cover - journalise l'erreur sur la fiche
        SocialVideo.objects.filter(pk=pk).update(status="error", error=str(e)[:500])
    finally:
        close_old_connections()


def enqueue(video):
    _executor.submit(_job, video.pk)


def mark_stale():
    """Un redemarrage du service tue les rendus en cours : on les signale au lieu de les laisser « en cours »."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import SocialVideo
    SocialVideo.objects.filter(status__in=["queued", "running"],
                               updated_at__lt=timezone.now() - timedelta(minutes=STALE_MINUTES)) \
        .update(status="error", error="Rendu interrompu (redémarrage du service). Relancez-le.")
