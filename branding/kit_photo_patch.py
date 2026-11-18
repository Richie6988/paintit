"""Remplace le logo imprime sur la notice de la photo du kit (accueil). Usage : python branding/kit_photo_patch.py <dossier>
<dossier>/kit_orig.jpg = photo d'origine (historique git, avant le commit du nouveau logo) ; ecrit <dossier>/kit_v2.png."""
import cv2, numpy as np, io, sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter
S = sys.argv[1]
FONT = "/home/user/paintit/studio/static/studio/fonts/Baloo2-Bold.ttf"
im = cv2.imread(S + "/kit_orig.jpg")
# 1. efface l'ancien logo (papier reconstitue)
x0, y0, x1, y1 = 386, 746, 463, 857
mask = np.zeros(im.shape[:2], np.uint8); mask[y0:y1, x0:x1] = 255
clean = cv2.inpaint(im, mask, 8, cv2.INPAINT_TELEA)
blur = cv2.GaussianBlur(clean, (0, 0), 7)
feather = cv2.GaussianBlur(mask.astype(np.float32) / 255, (0, 0), 3)[..., None]
clean = (clean * (1 - feather) + blur * feather).astype(np.uint8)
# 2. logo vertical, construit a 10x puis reduit (anti-crenelage propre)
K = 10
src = Image.open("/home/user/paintit/branding/logo_paintit.png").convert("RGBA")
icon = src.crop(src.getchannel("A").getbbox())
ICON_W = 36                                   # = largeur du QR code (35 px) : meme colonne
icon = icon.rotate(90, expand=True); iw = ICON_W * K; icon = icon.resize((iw, round(icon.height * iw / icon.width)), Image.LANCZOS)
font = ImageFont.truetype(FONT, 15 * K)       # texte « PaintIt » ~ hauteur de l'ancien
d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
bb = d0.textbbox((0, 0), "PaintIt", font=font); wp = d0.textlength("Paint", font=font)
txt = Image.new("RGBA", (bb[2] - bb[0] + 4 * K, bb[3] - bb[1] + 4 * K), (0, 0, 0, 0))
dt = ImageDraw.Draw(txt)
dt.text((2 * K - bb[0], 2 * K - bb[1]), "Paint", font=font, fill=(18, 34, 79, 255))
dt.text((2 * K - bb[0] + wp, 2 * K - bb[1]), "It", font=font, fill=(47, 107, 242, 255))
txt = txt.crop(txt.getchannel("A").getbbox()).rotate(90, expand=True)       # se lit de bas en haut
gap = 5 * K
H = txt.height + gap + icon.height; W = max(txt.width, icon.width)
strip = Image.new("RGBA", (W, H), (0, 0, 0, 0))
strip.alpha_composite(txt, ((W - txt.width) // 2, 0))
strip.alpha_composite(icon, ((W - icon.width) // 2, txt.height + gap))
strip = strip.resize((round(W / K), round(H / K)), Image.LANCZOS)
# 3. aspect « imprime puis photographie » : teinte du papier + legere douceur de l'objectif
base = Image.fromarray(cv2.cvtColor(clean, cv2.COLOR_BGR2RGB)).convert("RGBA")
QR_CX = (414 + 449) / 2.0
px = int(round(QR_CX - strip.width / 2)); py = 760
paper = np.asarray(base.crop((px, py, px + strip.width, py + strip.height)).convert("RGB"), np.float32) / 255
arr = np.asarray(strip, np.float32)
arr[..., :3] = arr[..., :3] * paper          # encre multipliee par le papier (eclairage de la photo)
strip = Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGBA").filter(ImageFilter.GaussianBlur(0.45))
base.alpha_composite(strip, (px, py))
out = base.convert("RGB")
print("logo", strip.size, "pos", px, py, "centre", px + strip.width / 2, "QR", QR_CX, "bas", py + strip.height)
out.save(S + "/kit_v2.png")
out.crop((370, 670, 480, 890)).resize((550, 1100), Image.LANCZOS).save(S + "/kit_v2_zoom.png")
