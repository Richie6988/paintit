"""Logo PaintIt sur la notice de la photo du kit, en PERSPECTIVE : le QR code (carre sur la feuille) donne la
transformation feuille -> photo ; le logo est dessine a plat dans le repere de la feuille puis projete.
Usage : kit_patch3.py <dossier>  (<dossier>/kit_orig.jpg = photo d'origine) -> <dossier>/kit_v3.png"""
import sys, cv2, numpy as np
from PIL import Image, ImageDraw, ImageFont
D = sys.argv[1]
FONT = "/home/user/paintit/studio/static/studio/fonts/Baloo2-Bold.ttf"
LOGO = "/home/user/paintit/branding/logo_paintit.png"
im = cv2.imread(D + "/kit_orig.jpg")
H_, W_ = im.shape[:2]
# 1. repere de la feuille : QR = carre 100 x 100 (origine coin haut-gauche)
roi = im[680:770, 400:465]
ok, q = cv2.QRCodeDetector().detect(cv2.resize(roi, None, fx=8, fy=8, interpolation=cv2.INTER_CUBIC))
assert ok
q = q.reshape(-1, 2) / 8 + [400, 680]                     # BL, TL, TR, BR (ordre du detecteur)
bl, tl, tr, br = q
sheet = np.float32([[0, 100], [0, 0], [100, 0], [100, 100]])
A, _ = cv2.estimateAffine2D(sheet, np.float32([bl, tl, tr, br]))
print("affine", A.round(4).tolist())
# 2. efface l'ancien logo (pixels colores / sombres sous le QR), papier reconstitue
reg = np.zeros((H_, W_), np.uint8); reg[733:855, 384:466] = 1
px = im.astype(int); sat = px.max(2) - px.min(2); dark = px.sum(2) < 560
ink = ((sat > 22) | dark) & (reg > 0)
mask = cv2.dilate(ink.astype(np.uint8) * 255, np.ones((5, 5), np.uint8), iterations=2)
clean = cv2.inpaint(im, mask, 9, cv2.INPAINT_TELEA)
sm = cv2.GaussianBlur(clean, (0, 0), 5)
f = cv2.GaussianBlur((mask > 0).astype(np.float32), (0, 0), 3)[..., None]
clean = (clean * (1 - f) + sm * f).astype(np.uint8)
# 3. logo vertical dessine a plat (unites feuille : largeur 100 = largeur du QR), haute resolution
U = 8                                                      # pixels par unite feuille
src = Image.open(LOGO).convert("RGBA"); icon = src.crop(src.getchannel("A").getbbox()).rotate(90, expand=True)
IW = 112; iw = IW * U; icon = icon.resize((iw, round(icon.height * iw / icon.width)), Image.LANCZOS)
font = ImageFont.truetype(FONT, 62 * U)
d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1))); bb = d0.textbbox((0, 0), "PaintIt", font=font); wp = d0.textlength("Paint", font=font)
txt = Image.new("RGBA", (bb[2] - bb[0] + 20 * U, bb[3] - bb[1] + 20 * U), (0, 0, 0, 0)); dt = ImageDraw.Draw(txt)
dt.text((10 * U - bb[0], 10 * U - bb[1]), "Paint", font=font, fill=(18, 34, 79, 255))
dt.text((10 * U - bb[0] + wp, 10 * U - bb[1]), "It", font=font, fill=(47, 107, 242, 255))
txt = txt.crop(txt.getchannel("A").getbbox()).rotate(90, expand=True)
gap = 16 * U
strip = Image.new("RGBA", (iw, txt.height + gap + icon.height), (0, 0, 0, 0))
strip.alpha_composite(txt, ((iw - txt.width) // 2, 0)); strip.alpha_composite(icon, (0, txt.height + gap))
Y0 = 100 + 86                                             # debut sous le QR (meme ecart que l'ancien logo)
print("bande feuille : 100 x %.0f unites, de y=%d" % (strip.height / U, Y0))
# 4. projection : pixel bande (u,v) -> feuille (u/U, Y0 + v/U) -> photo (A), rendue a SS x puis reduite
SS = 4
M = np.vstack([A, [0, 0, 1]]) @ np.array([[1 / U, 0, (100 - IW) / 2.0], [0, 1 / U, Y0], [0, 0, 1]])   # centre sur l'axe du QR
M = np.diag([SS, SS, 1]) @ M
rgba = np.asarray(strip, np.float32) / 255
pm = np.dstack([rgba[..., :3] * rgba[..., 3:], rgba[..., 3:]])        # alpha premultiplie
x0, y0, x1, y1 = 370, 720, 480, 880                       # zone de travail (photo)
T = np.array([[1, 0, -x0 * SS], [0, 1, -y0 * SS], [0, 0, 1]]) @ M
big = cv2.warpAffine(pm, T[:2], ((x1 - x0) * SS, (y1 - y0) * SS), flags=cv2.INTER_LINEAR, borderValue=0)
small = cv2.resize(big, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA)
small = cv2.GaussianBlur(small, (0, 0), 0.45)            # douceur de l'objectif de la photo
a = small[..., 3:]; col = np.where(a > 1e-4, small[..., :3] / np.maximum(a, 1e-4), 0)
base = cv2.cvtColor(clean, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
paper = base[y0:y1, x0:x1]
ink_rgb = col * paper                                     # encre x papier (eclairage)
base[y0:y1, x0:x1] = paper * (1 - a) + ink_rgb * a
out = Image.fromarray((base * 255).clip(0, 255).astype(np.uint8))
out.save(D + "/kit_v3.png")
out.crop((360, 660, 490, 890)).resize((520, 920), Image.LANCZOS).save(D + "/kit_v3_zoom.png")
