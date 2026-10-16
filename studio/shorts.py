"""Formats courts TikTok (12-21 s) pour gagner des abonnes : chaque format donne une raison d'interagir.
  - guess  : « Devine l'image » — petites zones d'abord, compte a rebours, revelation (commentaires, serie a suivre) ;
  - versus : « 12 vs 48 couleurs » — la meme photo en deux niveaux, peinte en parallele (choix gauche/droite) ;
  - tips   : « 3 astuces » — conseils utiles pendant que la toile se peint couleur par couleur (enregistrements).
Memes regles que satisfying.py : toiles du catalogue / studio / images importees, jamais une photo client.
"""
import math
import os
import re
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import satisfying as S

W, H, FPS, TOP = S.W, S.H, S.FPS, S.TOP
EDGE = np.array([190, 196, 210], np.uint8)
NUM_INK = np.array([96, 104, 122], np.float32)
ACCENT = (47, 107, 242)

FORMATS = [("fill", "Remplissage satisfying (classique)"), ("guess", "Devine l'image (12-15 s)"),
           ("versus", "12 vs 48 couleurs (15-17 s)"), ("tips", "3 astuces utiles (20-22 s)")]
DEFAULT_DURATION = {"guess": 14.0, "versus": 16.0, "tips": 21.0}
GUESS_INTRO = 1.6
DEFAULT_HOOK = {"guess": "Guess it before the countdown ends",
                "versus": "{lo} colors vs {hi} colors: which one would you paint?",
                "tips": "3 tips for your first paint by numbers"}
# Conseils verifiables, sans promesse : utiles meme a qui n'achete pas (enregistrements -> portee)
TIPS = [
    "Paint one colour at a time: fewer brush washes, no mix-ups",
    "Start at the top so your hand never smudges wet paint",
    "Close each pot right after use: the paint dries fast",
    "Light colour over a dark number? Let it dry, then add a second coat",
    "Use the very tip of the brush for tiny zones",
    "Lost? The reference poster in the kit shows the finished picture",
    "A small mistake? Paint over it once it's dry, nobody will know",
    "Wipe the extra paint off your brush: thin coats look cleaner",
    "20 minutes a day adds up faster than you think",
]
TITLES = {
    "guess": ["guess the picture 👀", "comment before the reveal", "i bet you can't guess this one",
              "guess it before the countdown", "how fast did you get it?"],
    "versus": ["left or right? 👇", "12 or 48 colors, be honest", "which one would you paint?",
               "easy mode vs expert mode", "the right one is chaos"],
    "tips": ["save this before your first paint by numbers 📌", "3 things i wish i knew before",
             "paint by numbers tips nobody tells you", "beginner tips 🎨", "do this on your first canvas"],
}


# ---------------- Peinture d'une toile (reutilisable : une ou plusieurs toiles par video) ----------------
class Painter:
    def __init__(self, uid, box_w, box_h, numbers=True, seed=0):
        labels, colors, nums, _areas, edge = S.load_zones(uid, box_w, box_h)
        self.uid, self.n = uid, len(colors)
        n = self.n
        self.ch, self.cw = ch, cw = labels.shape
        self.nums, self.col = nums, colors.astype(np.float32)
        self.lab = np.where(labels < 0, n, labels).astype(np.int32)
        flat = self.lab.ravel()
        self.px = np.bincount(flat, minlength=n + 1)[:n].astype(np.float64)
        yy, xx = np.mgrid[0:ch, 0:cw]
        self.cx = np.bincount(flat, xx.ravel(), n + 1)[:n] / np.maximum(self.px, 1)
        self.cy = np.bincount(flat, yy.ravel(), n + 1)[:n] / np.maximum(self.px, 1)
        dnorm = S.spread_dist(self.lab, edge, n)
        order = np.argsort(flat, kind="stable")
        b = np.searchsorted(flat[order], np.arange(n + 2))
        self.zone_px = [order[b[z]:b[z + 1]] for z in range(n)]
        self.zone_d = [dnorm[i][:, None] for i in self.zone_px]
        self.edge_idx = np.nonzero(edge.ravel())[0]
        self.edge_lab = flat[self.edge_idx]
        self.numbers = numbers
        if numbers:
            self.num_idx, self.num_a = S._number_layer(uid, cw, ch, min_px=8)
            self.num_lab = flat[self.num_idx]
        self.diag = ((xx + yy * 0.7) / (cw + ch * 0.7)).astype(np.float32)
        self.iri = S._rainbow(self.diag * 1.6)
        rng = np.random.default_rng(seed + 7)
        self.sparks = [(int(rng.uniform(0, max(1, cw - 56))), int(rng.uniform(0, max(1, ch - 56))),
                        rng.uniform(0.2, 2.2)) for _ in range(max(6, cw * ch // 70000))]
        self.pal = np.full((n + 1, 3), 255, np.float32)
        self.dur = np.clip(0.16 + 0.0011 * np.sqrt(self.px), 0.16, 0.65)

    @property
    def n_colors(self):
        return len(np.unique(self.nums))

    def canvas(self, t, start):
        """-> (image HxWx3 uint8, zones terminees)."""
        n = self.n
        a = np.clip((t - start) / self.dur, 0, 1)
        ae = a * a * (3 - 2 * a)
        since = t - (start + self.dur)
        flash = np.where(since > 0, np.exp(-np.maximum(since, 0) / 0.2), 0.0) * 0.45
        done = ae >= 1
        self.pal[:n] = np.where(done[:, None], self.col + (255 - self.col) * flash[:, None], 255)
        img = self.pal.astype(np.uint8)[self.lab]
        cf = img.reshape(-1, 3)
        for z in np.nonzero((ae > 0) & (ae < 1))[0]:
            m = np.clip((ae[z] * 1.08 - self.zone_d[z]) / 0.06, 0, 1)
            v = 255 + (self.col[z] - 255) * m
            v += (255 - v) * (m * (1 - m) * 2.0)
            cf[self.zone_px[z]] = v.astype(np.uint8)
        nd = int(done.sum())
        if nd < n:
            ae1 = np.append(ae, 1.0)
            cf[self.edge_idx[ae1[self.edge_lab] < 1]] = EDGE
            if self.numbers:
                wn = (self.num_a * np.clip(1 - ae1[self.num_lab] * 1.6, 0, 1))[:, None]
                cf[self.num_idx] = (cf[self.num_idx] * (1 - wn) + NUM_INK * wn).astype(np.uint8)
        return img, nd

    def shine(self, img, te):
        """Fin « win » : balayages irises + scintillements (te = secondes depuis la fin)."""
        if te <= 0:
            return img
        cv = img.astype(np.float32)
        kk = np.zeros(self.diag.shape, np.float32)
        for t0, amp in ((0.0, 0.85), (1.3, 0.45)):
            c = (te - t0) / 1.1 * 1.6 - 0.3
            if -0.4 < c < 1.4:
                kk += amp * np.exp(-((self.diag - c) / 0.09) ** 2)
        kk = np.minimum(kk + 0.10 * min(1.0, te / 0.6), 1)[..., None]
        cv = 255 - (255 - cv) * (1 - kk * self.iri / 255)
        star = _STAR
        for sx, sy, s0 in self.sparks:
            v = te - s0
            if 0 < v < 0.7:
                reg = cv[sy:sy + 56, sx:sx + 56]
                reg += (255 - reg) * star[:reg.shape[0], :reg.shape[1]] * math.sin(math.pi * v / 0.7)
        return np.clip(cv, 0, 255).astype(np.uint8)


_STAR = S._star()


def curve_times(rank, n, t0, span):
    """Rythme qui accelere sans cesse (comme le format classique) : rang -> instant de debut."""
    u = np.linspace(0, 1, 2001)
    return t0 + span * np.interp(np.asarray(rank) / max(1, n), 0.3 * u + 0.7 * u ** 3, u)


# ---------------- Calques texte ----------------
def _bubbles(items, maxw=W - 160):
    """[(texte, taille, couleur)] -> RGBA : une bulle blanche arrondie par ligne, centree (code TikTok)."""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    rows = []
    for text, size, color in items:
        f = S._font(size)
        for line in S._wrap(probe, S._clean(text), f, maxw)[:3]:
            l, t, r, b = probe.textbbox((0, 0), line, font=f)
            rows.append((line, f, color, l, t, r - l, int(size * 1.36)))
    if not rows:
        return np.zeros((1, 1, 4), np.uint8)
    w = max(r[5] for r in rows) + 48
    h = sum(r[6] for r in rows) + 4
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    y = 0
    for line, f, color, l, t, bw, lh in rows:
        px = (w - bw) / 2
        d.rounded_rectangle([px - 22, y, px + bw + 22, y + lh - 6], radius=18, fill=(255, 255, 255, 250))
        tb = d.textbbox((0, 0), line, font=f)
        d.text((px - l, y + (lh - 6 - (tb[3] - tb[1])) / 2 - tb[1]), line, font=f, fill=color)
        y += lh
    return np.array(im)


def _disc(txt, d=190):
    """Pastille ronde (compte a rebours)."""
    im = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.ellipse([4, 4, d - 5, d - 5], fill=(255, 255, 255, 245), outline=ACCENT, width=8)
    f = S._font(int(d * 0.62))
    l, t, r, b = dr.textbbox((0, 0), txt, font=f)
    dr.text(((d - (r - l)) / 2 - l, (d - (b - t)) / 2 - t), txt, font=f, fill=(10, 10, 10))
    return np.array(im)


def _put(frame, layer, cx, y, k=1.0, scale=1.0, anchor="top"):
    """Colle un calque RGBA centre horizontalement sur cx (y = haut, ou bas si anchor='bottom')."""
    import cv2
    if k <= 0.01:
        return
    if abs(scale - 1) > 0.01:
        layer = cv2.resize(layer, (max(1, int(layer.shape[1] * scale)), max(1, int(layer.shape[0] * scale))),
                           interpolation=cv2.INTER_LINEAR)
    h, w = layer.shape[:2]
    x = int(cx - w / 2)
    y = int(y - h) if anchor == "bottom" else int(y)
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    S._blend(frame, layer[y0 - y:y1 - y, x0 - x:x1 - x], x0, y0, k)


def _pop(since, dur=0.22):
    """Apparition « pop » : (alpha, echelle) avec leger rebond."""
    if since < 0:
        return 0.0, 1.0
    p = min(1.0, since / dur)
    return p, 0.75 + 0.25 * p + 0.08 * math.sin(math.pi * p)


def _next_series(series):
    m = re.search(r"(\d+)\s*$", series or "")
    return "#%d" % (int(m.group(1)) + 1) if m else ""


# ---------------- Rendu ----------------
def _base(rects):
    base = np.zeros((H, W, 3), np.uint8)
    base[:] = S.BG
    sh = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(sh)
    for ox, oy, cw, ch in rects:
        d.rounded_rectangle([ox - 4, oy + 8, ox + cw + 4, oy + ch + 18], radius=14, fill=60)
    sh = np.array(sh.filter(ImageFilter.GaussianBlur(16)), np.float32)[..., None] / 255.0
    return (base * (1 - sh * 0.5)).astype(np.uint8)


def _brand(frame, y, te):
    """Marque discrete sous la toile, irisee a la fin."""
    m = _BRAND
    bx = (W - m.shape[1]) // 2
    roi = frame[y:y + m.shape[0], bx:bx + m.shape[1]].astype(np.float32)
    if te > 0:
        col = S._rainbow(np.linspace(0, 1, m.shape[1], dtype=np.float32)[None, :] * 1.2 - te * 0.6, 20, 190)
    else:
        col = np.array(S.INK, np.float32)
    frame[y:y + m.shape[0], bx:bx + m.shape[1]] = (roi * (1 - m * 0.85) + col * m * 0.85).astype(np.uint8)


_BRAND = S._brand_mask()


def _ffmpeg(out_path, audio):
    import imageio_ffmpeg
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "%dx%d" % (W, H), "-r", str(FPS), "-i", "-"]
    if audio:
        cmd += ["-i", audio, "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def _plucks(p, start, skip=()):
    lum = p.col @ np.array([0.3, 0.59, 0.11], np.float32)
    idx = np.round(lum / 255 * (len(S.PENTA) - 1)).astype(int)
    amp = 0.45 + 0.55 * np.sqrt(p.px / max(p.px.max(), 1))
    return [(float(start[z]), int(idx[z]), float(amp[z])) for z in range(p.n) if z not in skip]


def render(fmt, uid, out_path, duration=0, hook="", answer="", series="", uid2="", seed=0, music_style="ambient",
           fill_sounds=True, show_numbers=True, progress=None):
    """Video MP4 au format court `fmt` (guess / versus / tips). -> chemin."""
    duration = float(duration or DEFAULT_DURATION[fmt])
    rng = np.random.default_rng(seed)
    nxt = _next_series(series)
    follow = ("Follow for %s" % nxt) if nxt else ("Follow for more tips" if fmt == "tips" else "Follow for the next one")
    fill_end_at = duration - (4.6 if fmt != "tips" else 4.4)

    if fmt == "versus":
        pw = (W - 3 * S.SIDE) // 2
        ps = [Painter(uid, pw, 1000, show_numbers, seed), Painter(uid2 or uid, pw, 1000, show_numbers, seed)]
        ps.sort(key=lambda p: (p.n_colors, p.n))          # la plus simple a gauche
        ch = max(p.ch for p in ps)
        oy = TOP + 150 + max(0, (760 - ch) // 2)
        pos = [(S.SIDE + (pw - ps[0].cw) // 2, oy), (2 * S.SIDE + pw + (pw - ps[1].cw) // 2, oy)]
    else:
        ps = [Painter(uid, W - 2 * S.SIDE, S.CANVAS_H, show_numbers, seed)]
        p = ps[0]
        pos = [((W - p.cw) // 2, TOP + (S.CANVAS_H - p.ch) // 2)]
    rects = [(x, y, p.cw, p.ch) for p, (x, y) in zip(ps, pos)]
    lo, hi = sorted((ps[0].n_colors, ps[-1].n_colors))
    hook = (hook or DEFAULT_HOOK[fmt]).replace("{lo}", str(lo)).replace("{hi}", str(hi))
    hook = S.hook_text(hook, ps[0].n, ps[0].n_colors)

    # --- calendrier de remplissage
    starts, ticks, tip_rows = [], [], []
    t0 = GUESS_INTRO if fmt == "guess" else 0.3            # devine : la regle est annoncee sur la toile vierge
    for p in ps:
        span = max(4.0, fill_end_at - t0 - 0.6)
        if fmt in ("guess", "tips"):       # couleur par couleur : l'image se construit par couches, mystere garde
            st = S.schedule(p.n, p.nums, "colors", span, seed) + t0
        else:
            st = S.schedule(p.n, p.nums, "flow", span, seed, p.px, np.c_[p.cx, p.cy]) + t0
        starts.append(st)
    fill_end = max(float(np.max(st + p.dur)) for p, st in zip(ps, starts))
    if fmt == "guess":
        ticks = [fill_end - 2.4, fill_end - 1.6, fill_end - 0.8]
    if fmt == "tips":
        picks = [TIPS[i] for i in rng.choice(len(TIPS), 3, replace=False)]
        title_t = 2.2
        step = (fill_end - title_t) / 3
        tip_rows = [(title_t + i * step, _bubbles([(tx, 54, (10, 10, 10))]), S._counter("Tip %d/3" % (i + 1)))
                    for i, tx in enumerate(picks)]

    # --- calques fixes
    base = _base(rects)
    hook_l = _bubbles([(hook, 56, (10, 10, 10))])
    if fmt == "guess":
        end_l = _bubbles([(answer or "Did you guess it?", 60, (10, 10, 10)), (follow, 46, ACCENT)])
    elif fmt == "versus":
        end_l = _bubbles([("Left or right? Tell me", 60, (10, 10, 10)), (follow, 46, ACCENT)])
    else:
        end_l = _bubbles([("Save this for your first canvas", 56, (10, 10, 10)), (follow, 46, ACCENT)])
    series_l = S._counter(("Guess " if fmt == "guess" else "") + series) if series else None
    discs = {s: _disc(s) for s in ("3", "2", "1")}
    vs_l = _disc("VS", 120)
    rule_l = _bubbles([("Can you guess", 76, (10, 10, 10)), ("what it is", 76, (10, 10, 10)),
                       ("before 3 - 2 - 1 ?", 64, ACCENT)])
    if fmt == "versus":
        labels = [_bubbles([("%d colors" % p.n_colors, 44, (10, 10, 10)), ("%d zones" % p.n, 34, (90, 98, 116))],
                           maxw=pw - 20) for p in ps]
    pills = {}
    brand_y = max(y + p.ch for p, (x, y) in zip(ps, pos)) + 34

    # --- son : musique + une note par zone (toile de gauche seulement en versus), bips du compte a rebours
    tmp_audio = out_path + ".wav"
    plucks = _plucks(ps[0], starts[0]) if fill_sounds else []
    plucks += [(t, len(S.PENTA) - 1, 1.0) for t in ticks]
    audio = None
    if music_style != "none" or plucks:
        S.music(tmp_audio, duration, fill_end, seed, style=music_style, plucks=plucks or None)
        audio = tmp_audio
    proc = _ffmpeg(out_path, audio)
    total = int(duration * FPS)
    first = None
    try:
        for fi in range(total):
            t = fi / FPS
            te = t - fill_end
            frame = base.copy()
            for i, (p, (x, y)) in enumerate(zip(ps, pos)):
                img, nd = p.canvas(t, starts[i])
                img = p.shine(img, te)
                frame[y:y + p.ch, x:x + p.cw] = img
                if fmt == "versus":
                    _put(frame, labels[i], x + p.cw / 2, y - 14, anchor="bottom")
                key = (i, nd)
                if key not in pills:
                    pills[key] = S._counter("%d / %d" % (nd, p.n) if fmt == "versus" else "%d / %d zones" % (nd, p.n))
                if te < 0.4:
                    S._blend(frame, pills[key], x + 14, y + p.ch - pills[key].shape[0] - 14, 1 - max(0, te) / 0.4)
            if fmt == "versus":
                _put(frame, vs_l, W / 2, pos[0][1] + ps[0].ch / 2 - vs_l.shape[0] / 2)
            x, y = pos[0]
            p = ps[0]
            # texte du haut : accroche -> (astuces) -> fin
            if te < 0.3:
                top_l, since = hook_l, t
                if tip_rows and t >= tip_rows[0][0]:
                    t_in, top_l, tag = [r for r in tip_rows if t >= r[0]][-1]
                    since = t - t_in
                    S._blend(frame, tag, x + 14, y + 14)
                k, sc = _pop(since)
                _put(frame, top_l, W / 2, TOP - 24, k * (1 - max(0, te) / 0.3), sc, anchor="bottom")
            else:
                k, sc = _pop(te - 0.3)
                _put(frame, end_l, W / 2, TOP - 24, k, sc, anchor="bottom")
            if series_l is not None and fmt == "guess" and te < 0:
                S._blend(frame, series_l, x + 14, y + 14)
            if fmt == "guess" and t < GUESS_INTRO + 0.3:      # annonce du defi, au centre, des la 1re image
                k, sc = _pop(t, 0.18)
                k *= min(1.0, (GUESS_INTRO + 0.3 - t) / 0.3)
                _put(frame, rule_l, W / 2, y + p.ch / 2 - rule_l.shape[0] / 2, k, sc)
                _put(frame, discs["3"], W / 2, y + p.ch / 2 + rule_l.shape[0] / 2 + 20, k, sc)
            for j, tk in enumerate(ticks):                   # compte a rebours 3-2-1
                if tk <= t < tk + 0.8 and te < 0:
                    k, sc = _pop(t - tk, 0.15)
                    _put(frame, discs[str(3 - j)], x + p.cw - 120, y + 24, k * min(1, (tk + 0.8 - t) / 0.15), sc)
            _brand(frame, brand_y, te)
            if first is None:
                first = frame.copy()
            elif t > duration - S.LOOP:                      # boucle sans couture
                kl = S._smooth((t - (duration - S.LOOP)) / S.LOOP)
                frame = (frame * (1 - kl) + first * kl).astype(np.uint8)
            proc.stdin.write(frame.tobytes())
            if progress and fi % 30 == 0:
                progress(fi / total)
        proc.stdin.close()
        proc.wait(timeout=900)
    finally:
        if os.path.exists(tmp_audio):
            os.remove(tmp_audio)
    if proc.returncode:
        raise RuntimeError("ffmpeg a échoué (code %s)" % proc.returncode)
    return out_path
