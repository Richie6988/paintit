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
INTRO, OUTRO = 0.3, 3.4                       # ca demarre tout de suite ; fin = toile finie + nom qui se colorie
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
    """Toiles utilisables (catalogue + studio creatif), jamais de commande client. -> [(uid, titre)]"""
    from .models import DigitalCanvas, MarketingAd
    out, seen = [], set()
    for c in DigitalCanvas.objects.filter(source="library").order_by("-created_at")[:300]:
        if c.uid not in seen and _svg_path(c.uid):
            seen.add(c.uid); out.append((c.uid, c.title or c.uid))
    for uid, camp in MarketingAd.objects.order_by("-created_at").values_list("image_uid", "campaign")[:300]:
        if uid not in seen and _svg_path(uid):
            seen.add(uid); out.append((uid, "Studio créatif · " + (camp or uid)))
    return out


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


def schedule(n_zones, numbers, mode, fill_seconds, seed=0, areas=None):
    """Instant de debut de remplissage de chaque zone (secondes, a partir de la fin de l'intro).
    Rythme : c(u) = N (0.3 u + 0.7 u^3) -> ca demarre tout de suite (part lineaire) et explose a la fin (part cubique)."""
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
    # aleatoire, mais les grandes zones sortent plutot en premier : la toile prend vie des la 1re seconde
    w = np.ones(n_zones) if areas is None else np.maximum(np.asarray(areas, float), 1e-6) ** 0.35
    order = np.argsort(-rng.random(n_zones) ** (1.0 / w))
    for rank, z in enumerate(order):
        start[z] = when(rank)
    return start


# ---------------- Musique generee (libre de droits) ----------------
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


def music(path, duration, fill_end, seed=0, sr=44100, style="ambient"):
    """Musique generee par le code (donc libre de droits), synchronisee sur le remplissage + « ding » final."""
    rng = np.random.default_rng(seed)
    out = np.zeros(int(duration * sr))
    if style == "electro":
        _electro(out, sr, duration, fill_end, rng)
    elif style == "space":
        _space(out, sr, duration, fill_end, rng)
    else:
        _ambient(out, sr, duration, fill_end)
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
    """Accroche en haut (RGBA pre-calcule)."""
    hook = _clean(hook)
    top = Image.new("RGBA", (W, TOP), (0, 0, 0, 0))
    d = ImageDraw.Draw(top)
    if hook:
        f = _font(62)
        lines = _wrap(d, hook, f, W - 120)[:3]
        y = 120 + (3 - len(lines)) * 36
        for l in lines:
            bw = d.textbbox((0, 0), l, font=f)[2]
            d.text(((W - bw) / 2, y), l, font=f, fill=INK)
            y += 74
    return np.array(top)


BRAND = "paintit.click"


def _brand_letters(colors):
    """Le nom sous la toile, lettre par lettre : version « a peindre » (contour) et version coloriee (palette de la toile)."""
    f = _font(124)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    l, t, r, b = probe.textbbox((0, 0), BRAND, font=f, stroke_width=4)
    bw, bh = r - l, b - t
    import colorsys
    # toutes les couleurs de la toile (sauf les trop claires, illisibles sur le fond), etalees par teinte
    pal = sorted({tuple(int(v) for v in c) for c in colors.astype(int) if c.sum() < 3 * 215},
                 key=lambda c: colorsys.rgb_to_hsv(*(v / 255 for v in c))[0])
    nl = sum(1 for ch in BRAND if ch not in " .")
    vivid = [pal[int(i * len(pal) / nl)] for i in range(nl)] if pal else [INK]
    empty = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(empty).text((-l, -t), BRAND, font=f, fill=(255, 255, 255), stroke_width=4, stroke_fill=(150, 158, 175))
    letters, k = [], 0
    for i, ch in enumerate(BRAND):
        if ch == " ":
            continue
        x0 = int(probe.textlength(BRAND[:i], font=f))
        im = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        c = vivid[k % len(vivid)] if ch != "." else INK
        ImageDraw.Draw(im).text((-l + x0, -t), ch, font=f, fill=c, stroke_width=4, stroke_fill=tuple(int(v * 0.6) for v in c))
        letters.append(np.array(im)); k += ch != "."
    return np.array(empty), letters


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


def hook_text(hook, n_zones, n_colors):
    """Remplace {zones} et {colors} par les vrais chiffres de la toile."""
    return (hook or "").replace("{zones}", str(n_zones)).replace("{colors}", str(n_colors))


def render(uid, out_path, mode="random", duration=65.0, hook="", end_title="", end_sub="", with_music=True,
           seed=0, progress=None, music_style="ambient"):
    """Genere la video MP4. -> chemin. `progress(f)` recoit 0..1."""
    import imageio_ffmpeg
    from PIL import ImageFilter
    labels, colors, numbers, areas, edge = load_zones(uid, W - 2 * SIDE, CANVAS_H)
    n = len(colors)
    ch, cw = labels.shape
    ox, oy = (W - cw) // 2, TOP + (CANVAS_H - ch) // 2
    fill_seconds = max(10.0, duration - INTRO - OUTRO)
    start = schedule(n, numbers, mode, fill_seconds, seed, areas) + INTRO
    fade = 0.22
    top = _text_layer(hook_text(hook, n, len(np.unique(numbers))))
    empty, letters = _brand_letters(colors)
    bx, by = (W - empty.shape[1]) // 2, oy + ch + 36
    col = colors.astype(np.float32)
    lab = np.where(labels < 0, n, labels)                       # index n = fond (blanc)
    edge_idx = np.nonzero(edge.ravel())[0]
    edge_rgb = np.array([190, 196, 210], np.uint8)
    base = np.zeros((H, W, 3), np.uint8); base[:] = BG
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle([ox - 4, oy + 8, ox + cw + 4, oy + ch + 18], radius=14, fill=60)
    sh = np.array(sh.filter(ImageFilter.GaussianBlur(16)), np.float32)[..., None] / 255.0
    base = (base * (1 - sh * 0.5)).astype(np.uint8)
    _blend(base, top, 0, 0); _blend(base, empty, bx, by)
    total_frames = int(duration * FPS)
    tmp_audio = out_path + ".wav"
    fill_end = float(start.max() + fade)
    with_music = with_music and music_style != "none"
    if with_music:
        music(tmp_audio, duration, fill_end, seed, style=music_style)
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", "-"]
    if with_music:
        cmd += ["-i", tmp_audio, "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    step = 0.11                                                 # une lettre du nom se colorie toutes les 0.11 s
    try:
        pal = np.zeros((n + 1, 3), np.float32); pal[n] = 255
        for fi in range(total_frames):
            t = fi / FPS
            a = np.clip((t - start) / fade, 0, 1).astype(np.float32)[:, None]
            pal[:n] = 255 * (1 - a) + col * a
            canvas = pal.astype(np.uint8)[lab]                        # gather uint8 : rapide
            done = t > fill_end
            if not done:
                canvas.reshape(-1, 3)[edge_idx] = edge_rgb            # contours des zones (sans numeros)
            frame = base.copy()
            frame[oy:oy + ch, ox:ox + cw] = canvas
            if t > fill_end + 0.2:                                    # fin : le nom se colorie lettre par lettre
                for i, layer in enumerate(letters):
                    k = min(1.0, (t - fill_end - 0.2 - i * step) / 0.18)
                    if k > 0:
                        _blend(frame, layer, bx, by, k)
            proc.stdin.write(frame.tobytes())
            if progress and fi % 30 == 0:
                progress(fi / total_frames)
        proc.stdin.close()
        proc.wait(timeout=600)
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
    "fr": "Une photo devient une peinture par numéros 🎨 Satisfaisant jusqu'à la dernière zone. Crée la tienne sur paintit.click (lien en bio)",
    "en": "A photo turned into a paint by numbers 🎨 Satisfying to the very last area. Make yours at paintit.click (link in bio)",
}
HASHTAGS = "#paintbynumbers #peintureparnumeros #satisfying #oddlysatisfying #asmr #artsatisfying #diyart #paintit"


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
               music_style=v.music_style, seed=v.seed, progress=prog)
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
