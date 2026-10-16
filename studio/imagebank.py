"""Banque d'images libres pour le catalogue, les videos TikTok et le studio creatif.

Sources (requetes faites depuis le serveur) :
  - Openverse (WordPress) : uniquement CC0 et domaine public (PDM) -> usage commercial libre, sans attribution ;
  - Wikimedia Commons : uniquement domaine public / CC0 ;
  - Pixabay, Pexels (cles API gratuites PIXABAY_KEY / PEXELS_KEY dans .env) : licences maison, usage commercial
    autorise, interdiction de revendre l'image telle quelle (la transformer en kit numerote = oeuvre modifiee).
Chaque image recoit un score d'eclat (« colorfulness » de Hasler & Suesstrunk, calcule sur la vignette) : seules les
images vives sont gardees. Les images ou des personnes apparaissent sont signalees (droit a l'image).
"""
import io
import os
import re
import threading
import time

import numpy as np
from django.conf import settings
from PIL import Image

UA = "PaintItImageBank/1.0 (+https://paintit.click; contact@paintit.click)"
MIN_SIDE = 1200          # cote minimal de l'image HD (impression 40x50 et videos 1080 px)
MIN_VIBRANCY = 35.0      # sous ce score, images ternes : ecartees d'office
THEMES = {
    "Animaux": ["tiger", "fox", "red panda", "lion", "elephant", "giraffe", "zebra", "deer", "horse", "cat", "dog",
                "kitten", "puppy", "chameleon", "frog", "owl"],
    "Oiseaux": ["parrot", "macaw", "toucan", "hummingbird", "kingfisher", "peacock", "flamingo", "bee-eater",
                "lorikeet", "puffin"],
    "Papillons & insectes": ["butterfly", "dragonfly", "ladybug", "bee on flower", "moth colorful"],
    "Fleurs": ["sunflower", "tulip field", "poppy", "lotus", "rose", "lavender field", "orchid", "cherry blossom",
               "dahlia", "wildflowers meadow"],
    "Paysages": ["autumn forest", "mountain lake", "waterfall", "rolling hills", "aurora borealis", "canyon",
                 "rice terraces", "desert dunes"],
    "Mer & plages": ["tropical beach", "turquoise sea", "lighthouse", "sailboat", "coral reef", "sea turtle",
                     "tropical fish", "whale"],
    "Couchers de soleil": ["sunset", "sunrise", "sunset sea", "sunset mountains", "golden hour"],
    "Villes & voyages": ["venice canal", "colorful houses", "santorini", "tuscany", "old town street",
                         "japanese temple", "hot air balloons", "night city lights"],
    "Espace": ["galaxy", "nebula", "milky way", "planet", "starry night sky"],
    "Cuisine & fruits": ["fruit", "citrus", "berries", "macarons", "vegetables market"],
    "Saisons": ["autumn leaves", "spring blossom", "winter snow landscape", "summer field"],
    "Abstrait": ["abstract colorful", "rainbow", "stained glass", "colorful pattern", "fireworks"],
    "Tableaux célèbres": ["van gogh", "monet", "hokusai", "renoir", "impressionism landscape",
                          "japanese woodblock print", "still life flowers", "klimt", "cezanne", "seurat"],
}
PROVIDERS = ("openverse", "wikimedia", "artic", "met", "pixabay", "pexels")
FREE = ("openverse", "wikimedia", "artic", "met")         # sans cle API
LICENSE_NOTES = {
    "openverse": "Domaine public / CC0 : usage commercial libre, aucune attribution requise.",
    "wikimedia": "Domaine public / CC0 : usage commercial libre, aucune attribution requise.",
    "artic": "Art Institute of Chicago : œuvres du domaine public, images CC0, usage commercial libre.",
    "met": "Metropolitan Museum (Open Access) : œuvres du domaine public, images CC0, usage commercial libre.",
    "pixabay": "Licence Pixabay : usage commercial autorisé sans attribution ; ne pas revendre l'image non modifiée.",
    "pexels": "Licence Pexels : usage commercial autorisé sans attribution ; ne pas revendre l'image non modifiée.",
}
PEOPLE_RX = re.compile(r"\b(people|person|woman|women|man|men|girl|boy|child|children|kid|portrait|face|selfie|"
                       r"bride|crowd|model|couple|baby)\b", re.I)


class RateLimited(Exception):
    """Quota de la source atteint (HTTP 429) pour un bon moment : on la met de cote pour cette collecte."""
    def __init__(self, wait):
        super().__init__("quota atteint, réessayer dans %d min" % max(1, wait // 60))
        self.wait = wait


def _get(s, url, **kw):
    """GET avec respect des 429 : attente courte (Retry-After <= 2 min) puis nouvel essai, sinon RateLimited."""
    for attempt in range(3):
        r = s.get(url, **kw)
        if getattr(r, "status_code", 200) != 429:
            r.raise_for_status()
            return r
        try:
            wait = int((getattr(r, "headers", None) or {}).get("Retry-After") or 60)
        except ValueError:
            wait = 60
        if wait > 120 or attempt == 2:
            raise RateLimited(wait)
        time.sleep(wait + 1)


def _openverse_token(s):
    """Jeton Openverse (compte gratuit : quotas bien plus larges qu'en anonyme). None sans identifiants."""
    cid, sec = getattr(settings, "OPENVERSE_CLIENT_ID", ""), getattr(settings, "OPENVERSE_CLIENT_SECRET", "")
    if not (cid and sec):
        return None
    from django.core.cache import cache
    tok = cache.get("openverse_token")
    if not tok:
        r = s.post("https://api.openverse.org/v1/auth_tokens/token/", timeout=20,
                   data={"grant_type": "client_credentials", "client_id": cid, "client_secret": sec})
        r.raise_for_status()
        d = r.json()
        tok = d["access_token"]
        cache.set("openverse_token", tok, max(60, int(d.get("expires_in", 36000)) - 300))
    return tok


def openverse_register(email):
    """Cree un compte API Openverse gratuit -> dict (client_id, client_secret). Lien de validation envoye par e-mail."""
    r = _session().post("https://api.openverse.org/v1/auth_tokens/register/", timeout=20, json={
        "name": "PaintIt image bank %s" % time.strftime("%Y%m%d%H%M"), "email": email,
        "description": "PaintIt (paintit.click) : recherche d'images CC0 / domaine public pour des kits de peinture par numeros."})
    r.raise_for_status()
    return r.json()


def _probe_size(s, url):
    """Dimensions d'une image en ne lisant que son debut (en-tete JPEG/PNG)."""
    from PIL import ImageFile
    with s.get(url, timeout=20, stream=True) as r:
        r.raise_for_status()
        p = ImageFile.Parser()
        for chunk in r.iter_content(16384):
            p.feed(chunk)
            if p.image:
                return p.image.size
            if p.data is not None and len(p.data or b"") > 600000:
                break
    return 0, 0


def _session():
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def available():
    """Sources utilisables maintenant (cle presente)."""
    return [p for p in PROVIDERS if p in FREE
            or (p == "pixabay" and settings.PIXABAY_KEY) or (p == "pexels" and settings.PEXELS_KEY)]


# ---------------- Recherche par source -> dicts normalises ----------------
def _openverse(s, q, n):
    """Sans compte, Openverse refuse (401) plus de 20 resultats par page : on enchaine les pages de 20."""
    results = []
    tok = _openverse_token(s)
    size = 50 if tok else 20
    hdr = {"Authorization": "Bearer " + tok} if tok else {}
    for page in range(1, (n + size - 1) // size + 1):
        r = _get(s, "https://api.openverse.org/v1/images/", timeout=20, headers=hdr, params={
            "q": q, "license": "cc0,pdm", "page_size": min(size, n - len(results)), "page": page, "mature": "false",
            "size": "large", "category": "photograph,digitized_artwork"})
        batch = r.json().get("results", [])
        results += batch
        if len(batch) < size or len(results) >= n:
            break
        time.sleep(1.0)
    for it in results:
        yield {"ext_id": it["id"], "title": it.get("title") or q, "author": it.get("creator") or "",
               "page_url": it.get("foreign_landing_url") or "", "full_url": it.get("url") or "",
               "thumb_url": it.get("thumbnail") or it.get("url"), "width": it.get("width") or 0,
               "height": it.get("height") or 0, "license": (it.get("license") or "").upper().replace("PDM", "Domaine public"),
               "license_url": it.get("license_url") or "", "tags": [t.get("name", "") for t in it.get("tags") or []]}


def _wikimedia(s, q, n):
    r = _get(s, "https://commons.wikimedia.org/w/api.php", timeout=20, params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": min(n, 50),
        "gsrsearch": "%s filetype:bitmap" % q, "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime",
        "iiurlwidth": 480})
    r.raise_for_status()
    for page in ((r.json().get("query") or {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "")
        if not re.search(r"public domain|^pd|cc0", lic, re.I) or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        title = re.sub(r"^File:|\.\w+$", "", page.get("title", "")).replace("_", " ")
        author = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", ""))[:200]
        yield {"ext_id": str(page.get("pageid")), "title": title, "author": author,
               "page_url": info.get("descriptionurl", ""), "full_url": info.get("url", ""),
               "thumb_url": info.get("thumburl") or info.get("url", ""), "width": info.get("width") or 0,
               "height": info.get("height") or 0, "license": "Domaine public" if "cc0" not in lic.lower() else "CC0",
               "license_url": (meta.get("LicenseUrl") or {}).get("value", ""),
               "tags": [re.sub(r"<[^>]+>", "", (meta.get("Categories") or {}).get("value", ""))]}


def _pixabay(s, q, n):
    r = s.get("https://pixabay.com/api/", timeout=20, params={
        "key": settings.PIXABAY_KEY, "q": q, "image_type": "photo", "per_page": max(3, min(n, 200)),
        "safesearch": "true", "min_width": MIN_SIDE, "order": "popular"})
    r.raise_for_status()
    for it in r.json().get("hits", []):
        w, h = it.get("imageWidth") or 0, it.get("imageHeight") or 0
        scale = min(1.0, 1280 / max(w, h, 1))            # largeImageURL : 1280 px de cote max (API publique)
        yield {"ext_id": str(it["id"]), "title": q.capitalize(), "author": it.get("user") or "",
               "page_url": it.get("pageURL") or "", "full_url": it.get("largeImageURL") or "",
               "thumb_url": it.get("webformatURL") or "", "width": int(w * scale), "height": int(h * scale),
               "license": "Pixabay", "license_url": "https://pixabay.com/service/license-summary/",
               "tags": (it.get("tags") or "").split(", ")}


def _pexels(s, q, n):
    r = s.get("https://api.pexels.com/v1/search", timeout=20, headers={"Authorization": settings.PEXELS_KEY},
              params={"query": q, "per_page": min(n, 80)})
    r.raise_for_status()
    for it in r.json().get("photos", []):
        src = it.get("src") or {}
        yield {"ext_id": str(it["id"]), "title": it.get("alt") or q.capitalize(), "author": it.get("photographer") or "",
               "page_url": it.get("url") or "", "full_url": src.get("original") or "",
               "thumb_url": src.get("medium") or "", "width": it.get("width") or 0, "height": it.get("height") or 0,
               "license": "Pexels", "license_url": "https://www.pexels.com/license/", "tags": [it.get("alt") or ""]}


def _artic(s, q, n):
    """Art Institute of Chicago : oeuvres du domaine public (images CC0, IIIF)."""
    r = _get(s, "https://api.artic.edu/api/v1/artworks/search", timeout=20, headers={"AIC-User-Agent": UA}, params={
        "q": q, "limit": min(n, 100), "query[term][is_public_domain]": "true",
        "fields": "id,title,artist_title,image_id,thumbnail,is_public_domain,term_titles"})
    for it in r.json().get("data", []):
        th = it.get("thumbnail") or {}
        if not it.get("image_id") or not it.get("is_public_domain") or not th.get("width"):
            continue
        w = min(1686, th["width"])                       # taille IIIF recommandee par le musee
        h = int(w * th["height"] / th["width"])
        base = "https://www.artic.edu/iiif/2/%s/full/" % it["image_id"]
        yield {"ext_id": str(it["id"]), "title": it.get("title") or q, "author": it.get("artist_title") or "",
               "page_url": "https://www.artic.edu/artworks/%s" % it["id"], "full_url": base + "%d,/0/default.jpg" % w,
               "thumb_url": base + "400,/0/default.jpg", "width": w, "height": h, "license": "CC0",
               "license_url": "https://www.artic.edu/open-access", "tags": it.get("term_titles") or []}


def _met(s, q, n):
    """Metropolitan Museum Open Access : objets du domaine public avec image (CC0)."""
    r = _get(s, "https://collectionapi.metmuseum.org/public/collection/v1/search", timeout=20,
             params={"q": q, "hasImages": "true"})
    ids = (r.json().get("objectIDs") or [])[:n * 2]
    found = 0
    for oid in ids:
        if found >= n:
            break
        time.sleep(0.05)
        try:
            o = _get(s, "https://collectionapi.metmuseum.org/public/collection/v1/objects/%d" % oid, timeout=20).json()
        except RateLimited:
            raise
        except Exception:
            continue
        if not o.get("isPublicDomain") or not o.get("primaryImage"):
            continue
        try:
            w, h = _probe_size(s, o["primaryImage"])
        except Exception:
            continue
        found += 1
        yield {"ext_id": str(oid), "title": o.get("title") or q, "author": o.get("artistDisplayName") or "",
               "page_url": o.get("objectURL") or "", "full_url": o["primaryImage"],
               "thumb_url": o.get("primaryImageSmall") or o["primaryImage"], "width": w, "height": h, "license": "CC0",
               "license_url": "https://www.metmuseum.org/policies/image-resources",
               "tags": [o.get("classification") or "", o.get("objectName") or ""]}


SEARCH = {"openverse": _openverse, "wikimedia": _wikimedia, "artic": _artic, "met": _met, "pixabay": _pixabay,
          "pexels": _pexels}


# ---------------- Score d'eclat ----------------
def vibrancy(img):
    """Colorfulness (Hasler & Suesstrunk 2003), 0..~120 ; > 50 = tres colore. Ramene a 0-100."""
    a = np.asarray(img.convert("RGB").resize((160, 160)), np.float32)
    rg = a[..., 0] - a[..., 1]
    yb = 0.5 * (a[..., 0] + a[..., 1]) - a[..., 2]
    m = np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())
    return round(float(min(100.0, m)), 1)


# ---------------- Collecte ----------------
_KEY = "imagebank_state"


def state():
    """Avancement de la collecte (partage entre les processus du site via le cache)."""
    from django.core.cache import cache
    return cache.get(_KEY) or {"running": False, "done": 0, "total": 0, "added": 0, "errors": 0, "current": "",
                               "finished": ""}


def _set(**kw):
    from django.core.cache import cache
    st = state()
    st.update(kw)
    cache.set(_KEY, st, 24 * 3600)


def harvest(categories=None, providers=None, per_query=30, min_vibrancy=MIN_VIBRANCY, log=None, pause=1.0, info=None):
    """Interroge les sources pour chaque theme et enregistre les images vives et HD. -> (ajoutees, ignorees)."""
    from .models import StockImage
    s = _session()
    cats = [c for c in THEMES if not categories or c in categories]
    provs = [p for p in (providers or available()) if p in available()]
    added = skipped = 0
    blocked = set()
    os.makedirs(os.path.join(settings.MEDIA_ROOT, "stock", "thumbs"), exist_ok=True)
    for cat in cats:
        for q in THEMES[cat]:
            for prov in provs:
                if prov in blocked:
                    continue
                _set(current="%s · %s · %s" % (cat, q, prov))
                before = added
                try:
                    items = list(SEARCH[prov](s, q, per_query))
                except RateLimited as exc:                   # quota : source mise de cote, un seul message
                    blocked.add(prov)
                    _set(errors=state()["errors"] + 1)
                    if log:
                        log("%s : %s. Source ignorée pour le reste de cette collecte." % (prov, exc))
                    continue
                except Exception as exc:                     # source indisponible : on passe a la suivante
                    _set(errors=state()["errors"] + 1)
                    if log:
                        log("%s « %s » : %s" % (prov, q, exc))
                    continue
                known = set(StockImage.objects.filter(provider=prov, ext_id__in=[i["ext_id"] for i in items])
                            .values_list("ext_id", flat=True))
                for it in items:
                    if it["ext_id"] in known or not it["full_url"] or max(it["width"], it["height"]) < MIN_SIDE:
                        skipped += 1
                        continue
                    try:
                        r = s.get(it["thumb_url"], timeout=20)
                        r.raise_for_status()
                        im = Image.open(io.BytesIO(r.content)).convert("RGB")
                    except Exception:
                        skipped += 1
                        continue
                    score = vibrancy(im)
                    if score < min_vibrancy:
                        skipped += 1
                        continue
                    tags = " ".join(it["tags"])[:2000]
                    obj = StockImage.objects.create(
                        provider=prov, ext_id=it["ext_id"], title=(it["title"] or q)[:200], category=cat, query=q,
                        tags=tags, author=it["author"][:200], page_url=it["page_url"][:500],
                        full_url=it["full_url"][:800], license=it["license"][:40], license_url=it["license_url"][:300],
                        width=it["width"], height=it["height"], vibrancy=score,
                        people=bool(PEOPLE_RX.search(tags + " " + (it["title"] or ""))))
                    im.thumbnail((480, 480))
                    im.save(obj.thumb_path(), "JPEG", quality=84)
                    added += 1
                _set(done=state()["done"] + 1, added=added)
                if info:
                    info("%s · %s · %s : %d résultat(s), +%d image(s) (total %d)"
                         % (cat, q, prov, len(items), added - before, added))
                time.sleep(pause)                            # politesse envers les API gratuites
    return added, skipped


def harvest_async(**kw):
    """Collecte en arriere-plan depuis l'admin (une seule a la fois, tous processus confondus)."""
    from django.core.cache import cache
    if not cache.add("imagebank_lock", 1, 6 * 3600):
        return False
    _set(running=True, done=0, total=total_queries(kw.get("categories"), kw.get("providers")), added=0, errors=0,
         current="", finished="")

    def run():
        from django.db import close_old_connections
        from django.utils import timezone
        close_old_connections()
        try:
            harvest(**kw)
        finally:
            _set(running=False, current="", finished=timezone.localtime().strftime("%d/%m %H:%M"))
            cache.delete("imagebank_lock")
            close_old_connections()
    threading.Thread(target=run, daemon=True, name="imagebank").start()
    return True


def total_queries(categories=None, providers=None):
    cats = [c for c in THEMES if not categories or c in categories]
    return sum(len(THEMES[c]) for c in cats) * len([p for p in (providers or available()) if p in available()])


# ---------------- Utilisation ----------------
def fetch_hd(img, max_side=3000):
    """Telecharge l'image HD (JPEG, plafonnee a max_side) dans media/uploads -> chemin local."""
    s = _session()
    r = s.get(img.full_url, timeout=60)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content))
    from PIL import ImageOps
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    path = os.path.join(settings.MEDIA_ROOT, "uploads", "stock-%d.jpg" % img.pk)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path, "JPEG", quality=93)
    return path


def mark_used(img, where):
    used = [u for u in img.used_in.split(", ") if u]
    if where not in used:
        used.append(where)
    img.used_in = ", ".join(used)[:300]
    if img.status == "new":
        img.status = "kept"
    img.save(update_fields=["used_in", "status"])
