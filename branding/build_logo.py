"""Genere les declinaisons du logo PaintIt a partir du PNG source (fond transparent)."""
import sys, io, base64, os
from PIL import Image, ImageDraw, ImageFont
SRC, IMG = sys.argv[1], sys.argv[2]
FONTS = os.path.join(os.path.dirname(IMG.rstrip("/")), "fonts") + "/"
src = Image.open(SRC).convert("RGBA"); icon = src.crop(src.getchannel("A").getbbox())
def fit(im, h): return im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS)
def square(im, side, pad=0.04, bg=(0, 0, 0, 0)):
    c = Image.new("RGBA", (side, side), bg); inner = int(side * (1 - 2 * pad))
    f = inner / max(im.size); r = im.resize((round(im.width * f), round(im.height * f)), Image.LANCZOS)
    c.alpha_composite(r, ((side - r.width) // 2, (side - r.height) // 2)); return c
def save(im, n):
    p = os.path.join(IMG, n); im.save(p, optimize=True); print(n, im.size, os.path.getsize(p) // 1024, "Ko")
ic = fit(icon, 160); save(ic, "icon.png")
b = io.BytesIO(); ic.save(b, "PNG", optimize=True)
open(os.path.join(IMG, "icon.svg"), "w").write(
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 %d %d">'
    '<image width="%d" height="%d" xlink:href="data:image/png;base64,%s"/></svg>'
    % (ic.width, ic.height, ic.width, ic.height, base64.b64encode(b.getvalue()).decode()))
save(square(icon, 192), "favicon.png")
save(square(icon, 180, pad=0.08, bg=(255, 255, 255, 255)), "apple-touch-icon.png")
H = 304; ic2 = fit(icon, H); font = ImageFont.truetype(FONTS + "Baloo2-Bold.ttf", 236)
d0 = ImageDraw.Draw(Image.new("RGBA", (10, 10))); wp = d0.textlength("Paint", font=font); wi = d0.textlength("It", font=font)
gap = 46; logo = Image.new("RGBA", (ic2.width + gap + int(wp + wi) + 8, H), (0, 0, 0, 0)); logo.alpha_composite(ic2, (0, 0))
d = ImageDraw.Draw(logo); bb = d.textbbox((0, 0), "PaintIt", font=font); y = (H - (bb[3] - bb[1])) // 2 - bb[1]; x = ic2.width + gap
d.text((x, y), "Paint", font=font, fill=(18, 34, 79, 255)); d.text((x + wp, y), "It", font=font, fill=(47, 107, 242, 255))
logo = logo.crop(logo.getchannel("A").getbbox())
save(logo.resize((800, round(logo.height * 800 / logo.width)), Image.LANCZOS), "logo.png")
