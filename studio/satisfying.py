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
INTRO, OUTRO = 1.2, 3.8                       # toile vide au debut, toile finie + carte de fin
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
    svg = open(_svg_path(uid), encoding="utf-8").read()
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


def schedule(n_zones, numbers, mode, fill_seconds, seed=0):
    """Instant de debut de remplissage de chaque zone (secondes, a partir de la fin de l'intro)."""
    rng = np.random.default_rng(seed)
    if mode == "colors":
        order_cols = list(dict.fromkeys(rng.permutation(np.unique(numbers)).tolist()))
        groups = [np.where(numbers == c)[0] for c in order_cols]
        # chaque couleur recoit un temps proportionnel a son nombre de zones, avec une acceleration globale
        start = np.zeros(n_zones)
        k = 1.8
        total = n_zones
        done = 0
        for g in groups:
            idx = rng.permutation(g)
            for j, z in enumerate(idx):
                frac = (done + j) / max(1, total)
                start[z] = fill_seconds * frac ** (1 / k)
            done += len(idx)
        return start
    order = rng.permutation(n_zones)
    k = 2.2                                     # c(t) = N (t/T)^k : lent au debut, tres rapide a la fin
    start = np.zeros(n_zones)
    for rank, z in enumerate(order):
        start[z] = fill_seconds * (rank / max(1, n_zones)) ** (1 / k)
    return start


def music(path, duration, fill_end, seed=0, sr=44100):
    """Musique d'ambiance generee (libre de droits) : nappe d'accords + arpege dont le tempo suit le remplissage."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(duration * sr)) / sr
    out = np.zeros_like(t)
    prog = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]   # Am F C G
    hz = lambda m: 440.0 * 2 ** ((m - 69) / 12)
    bar = 4.0
    for i, start in enumerate(np.arange(0, duration, bar)):
        chord = prog[i % len(prog)]
        a, b = int(start * sr), int(min(duration, start + bar + 1.0) * sr)
        tt = t[a:b] - start
        env = np.minimum(1, tt / 0.8) * np.exp(-np.maximum(0, tt - bar) * 3)
        for m in chord:
            out[a:b] += 0.05 * env * (np.sin(2 * np.pi * hz(m) * tt) + 0.3 * np.sin(2 * np.pi * hz(m) * 2 * tt))
    # arpege : intervalle entre notes de 0.5 s a 0.12 s a mesure que la toile se remplit
    pos = 0.6
    step = 0
    while pos < duration - 0.3:
        prog_i = int(pos // bar) % len(prog)
        m = prog[prog_i][step % 3] + 12 * (1 + (step // 3) % 2)
        a = int(pos * sr); n = int(0.6 * sr)
        tt = np.arange(min(n, len(t) - a)) / sr
        out[a:a + len(tt)] += 0.07 * np.exp(-tt * 6) * np.sin(2 * np.pi * hz(m) * tt)
        frac = min(1.0, pos / max(1e-3, fill_end))
        pos += 0.5 - 0.38 * frac ** 2 if pos < fill_end else 0.5
        step += 1
    # « ding » final
    a = int(fill_end * sr)
    tt = np.arange(min(int(2.5 * sr), len(t) - a)) / sr
    for m in (72, 76, 79, 84):
        out[a:a + len(tt)] += 0.06 * np.exp(-tt * 2.2) * np.sin(2 * np.pi * hz(m) * tt)
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


def _text_layer(hook, end_title, end_sub):
    """Calques texte pre-calcules (RGBA) : accroche en haut, marque en bas, carte de fin."""
    hook, end_title, end_sub = _clean(hook), _clean(end_title), _clean(end_sub)
    top = Image.new("RGBA", (W, 330), (0, 0, 0, 0))
    d = ImageDraw.Draw(top)
    if hook:
        f = _font(64)
        lines = _wrap(d, hook, f, W - 140)
        y = 150                                 # sous la barre « Abonnements | Pour toi » de TikTok
        for l in lines[:2]:
            bw = d.textbbox((0, 0), l, font=f)[2]
            d.text(((W - bw) / 2, y), l, font=f, fill=INK)
            y += 78
    bottom = Image.new("RGBA", (W, 80), (0, 0, 0, 0))
    d = ImageDraw.Draw(bottom)
    f = _font(46)
    txt = "paintit.click"
    bw = d.textbbox((0, 0), txt, font=f)[2]
    d.text(((W - bw) / 2, 8), txt, font=f, fill=(*INK, 230))
    # carte de fin dans la zone sure (TikTok masque ~150 px en haut, ~400 px en bas et la colonne de droite)
    end = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(end)
    cy = 960
    d.rounded_rectangle([60, cy, W - 60, cy + 540], radius=40, fill=(18, 34, 79, 240))
    f1, f2 = _font(84), _font(50)
    y = cy + 50
    for l in _wrap(d, end_title, f1, W - 120)[:2]:
        bw = d.textbbox((0, 0), l, font=f1)[2]
        d.text(((W - bw) / 2, y), l, font=f1, fill=(255, 255, 255)); y += 100
    for l in _wrap(d, end_sub, f2, W - 140)[:2]:
        bw = d.textbbox((0, 0), l, font=f2)[2]
        d.text(((W - bw) / 2, y + 10), l, font=f2, fill=(205, 218, 245)); y += 64
    pill = "paintit.click"
    fp = _font(60)
    bw = d.textbbox((0, 0), pill, font=fp)[2]
    px, py = (W - bw - 90) / 2, cy + 380
    d.rounded_rectangle([px, py, px + bw + 90, py + 110], radius=55, fill=(47, 107, 242))
    d.text((px + 45, py + 14), pill, font=fp, fill=(255, 255, 255))
    return np.array(top), np.array(bottom), np.array(end)


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


def _blend(frame, layer, x, y):
    h, w = layer.shape[:2]
    a = layer[..., 3:4].astype(np.float32) / 255.0
    roi = frame[y:y + h, x:x + w].astype(np.float32)
    frame[y:y + h, x:x + w] = (roi * (1 - a) + layer[..., :3] * a).astype(np.uint8)


def render(uid, out_path, mode="random", duration=65.0, hook="", end_title="", end_sub="", with_music=True,
           seed=0, progress=None):
    """Genere la video MP4. -> chemin. `progress(f)` recoit 0..1."""
    import imageio_ffmpeg
    box_w, box_h = W - 120, H - 330 - 520
    labels, colors, numbers, areas, edge = load_zones(uid, box_w, box_h)
    n = len(colors)
    ch, cw = labels.shape
    ox, oy = (W - cw) // 2, 330
    fill_seconds = max(10.0, duration - INTRO - OUTRO)
    start = schedule(n, numbers, mode, fill_seconds, seed) + INTRO
    fade = 0.22
    top, bottom, end = _text_layer(hook, end_title, end_sub)
    col = colors.astype(np.float32)
    lab = np.where(labels < 0, n, labels)                       # index n = fond (blanc)
    edge_idx = np.nonzero(edge.ravel())[0]
    edge_rgb = np.array([190, 196, 210], np.uint8)
    base = np.zeros((H, W, 3), np.uint8); base[:] = BG
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).rounded_rectangle([ox - 6, oy + 10, ox + cw + 6, oy + ch + 22], radius=18, fill=60)
    from PIL import ImageFilter
    sh = np.array(sh.filter(ImageFilter.GaussianBlur(18)), np.float32)[..., None] / 255.0
    base = (base * (1 - sh * 0.5)).astype(np.uint8)
    by = min(oy + ch + 28, H - 480)
    base_top = base.copy(); _blend(base_top, top, 0, 0); _blend(base_top, bottom, 0, by)
    base_end = base.copy()
    total_frames = int(duration * FPS)
    tmp_audio = out_path + ".wav"
    fill_end = float(start.max() + fade)
    if with_music:
        music(tmp_audio, duration, fill_end, seed)
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", "-"]
    if with_music:
        cmd += ["-i", tmp_audio, "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
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
            frame = (base_top if not done else base_end).copy()
            frame[oy:oy + ch, ox:ox + cw] = canvas
            if t > fill_end + 0.6:
                k = min(1.0, (t - fill_end - 0.6) / 0.5)
                layer = end.copy(); layer[..., 3] = (layer[..., 3] * k).astype(np.uint8)
                _blend(frame, layer, 0, 0)
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
        render(v.canvas_uid, tmp, mode=v.mode, duration=v.duration, hook=v.hook, end_title=v.end_title,
               end_sub=v.end_sub, with_music=v.music, seed=v.seed, progress=prog)
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
