"""Visuels prets d'office pour la bibliotheque de publications.

Chaque modele (PostTemplate) recoit un visuel genere par le Marketing Corner a partir d'une toile du catalogue
(photo -> toile coloriee -> toile numerotee), avec son accroche en titre. Le visuel est une variante MarketingAd
ordinaire : « Modifier » l'ouvre dans le Marketing Corner (photo, textes, police, couleurs) et la nouvelle
variante remplace l'ancienne dans la bibliotheque.
"""
import logging
import os
import re
import secrets
import threading

from django.conf import settings

logger = logging.getLogger(__name__)
CTA = {"fr": "Créez la vôtre", "en": "Create yours", "de": "Jetzt gestalten", "es": "Crea la tuya", "it": "Crea la tua"}
_lock = threading.Lock()


def _has_files(uid):
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    import glob
    return os.path.exists(os.path.join(d, "%s_preview.svg" % uid)) and \
        os.path.exists(os.path.join(d, "%s_preview.png" % uid)) and bool(glob.glob(os.path.join(d, "%s_source_*" % uid)))


def base_canvases():
    """Toiles utilisables comme photo de base : catalogue d'abord, puis visuels marketing deja generes."""
    from .models import DigitalCanvas, MarketingAd
    out, seen = [], set()
    for c in DigitalCanvas.objects.filter(source__in=("library", "gift")).order_by("-created_at")[:200]:
        if c.uid not in seen and _has_files(c.uid):
            seen.add(c.uid)
            out.append((c.uid, "%s %s" % (c.title, c.category)))
    for uid in MarketingAd.objects.values_list("image_uid", flat=True).distinct()[:100]:
        if uid not in seen and _has_files(uid):
            seen.add(uid)
            out.append((uid, ""))
    return out


def _words(t):
    return {w for w in re.findall(r"[a-zà-ÿ]{4,}", (t or "").lower())}


def pick_canvas(tpl, cands):
    """Toile dont le titre/categorie partage le plus de mots avec le modele ; sinon rotation stable."""
    if not cands:
        return None
    words = _words("%s %s %s" % (tpl.hook, tpl.media_brief, tpl.name))
    best = max(cands, key=lambda c: len(words & _words(c[1])))
    if words & _words(best[1]):
        return best[0]
    return cands[(tpl.pk or 0) % len(cands)][0]


def _trim(text, n=90):
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def build(tpl, uid=None):
    """Genere le visuel du modele (format adapte) et le rattache. -> MarketingAd ou None."""
    from . import marketing
    from .models import MarketingAd, PostTemplate
    uid = uid or pick_canvas(tpl, base_canvases())
    if not uid:
        return None
    variant = "L%d" % tpl.pk
    while MarketingAd.objects.filter(image_uid=uid, variant=variant).exists():
        variant += "b"
    kind = tpl.visual_kind()
    lang = (tpl.lang or "en")[:2]
    gif = {"gif_title": _trim(tpl.hook), "gif_cta": CTA.get(lang, CTA["en"]),
           "gif_footer": settings.SITE_URL.replace("https://", "").replace("http://", "")}
    suffix = "_" + variant.lower()
    produced = marketing.build_all(uid, gif_texts=gif, suffix=suffix, only={kind})
    ad = None
    for key, label, path, k in produced:
        ad = MarketingAd.objects.create(token=secrets.token_urlsafe(9), image_uid=uid, campaign="Bibliothèque",
                                        variant=variant, kind=key, label=label, fmt="gif" if k == "gif" else "web",
                                        file_name=os.path.basename(path),
                                        target_url=settings.SITE_URL + (tpl.target_path or "/create/"),
                                        texts=dict(gif, _style=marketing.norm_style(None)))
    if ad:
        PostTemplate.objects.filter(pk=tpl.pk).update(ad_uid=uid, ad_variant=variant)
    return ad


def build_missing(limit=60):
    """Genere les visuels manquants (modeles actifs, hors « texte seul »). -> nombre genere."""
    from .models import PostTemplate
    if not _lock.acquire(blocking=False):
        return 0
    try:
        cands = base_canvases()
        if not cands:
            return 0
        n = 0
        todo = [t for t in PostTemplate.objects.exclude(media_kind="text") if not t.visual()][:limit]
        for t in todo:
            try:
                if build(t, pick_canvas(t, cands)):
                    n += 1
            except Exception:
                logger.exception("Visuel bibliotheque %s", t.pk)
        return n
    finally:
        _lock.release()


def build_missing_async():
    threading.Thread(target=build_missing, daemon=True).start()
