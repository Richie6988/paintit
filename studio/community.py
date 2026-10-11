"""Communautes : annuaire, liens traques et suivi des publications (compte de marque assume, jamais de faux comptes).

Reddit : API publique (.json) ou OAuth « application only » si REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET sont definis
(app de type « script » creee sur https://www.reddit.com/prefs/apps). Lecture seule : membres, regles, score et
nombre de commentaires des publications. La publication elle-meme reste manuelle (regles des communautes).
"""
import base64
import json
import logging
import re
import secrets
import time
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)
UA = "web:paintit-community-monitor:1.0 (by PaintIt)"


def new_code():
    from .models import Outreach
    while True:
        c = secrets.token_urlsafe(5).replace("-", "").replace("_", "")[:7].lower()
        if len(c) >= 6 and not Outreach.objects.filter(code=c).exists():
            return c


def tracked_url(o):
    return "%s/go/%s/" % (settings.SITE_URL, o.code)


# ------------------------------------------------------------------ Reddit
def _token():
    cid, sec = getattr(settings, "REDDIT_CLIENT_ID", ""), getattr(settings, "REDDIT_CLIENT_SECRET", "")
    if not (cid and sec):
        return None
    tok = cache.get("reddit_app_token")
    if tok:
        return tok
    req = urllib.request.Request("https://www.reddit.com/api/v1/access_token",
                                 data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
                                 headers={"User-Agent": UA, "Authorization": "Basic " + base64.b64encode(
                                     ("%s:%s" % (cid, sec)).encode()).decode()})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode())
    tok = data.get("access_token")
    if tok:
        cache.set("reddit_app_token", tok, int(data.get("expires_in", 3600)) - 120)
    return tok


def _get(path):
    """GET JSON Reddit (OAuth si configure, sinon endpoint public)."""
    tok = None
    try:
        tok = _token()
    except Exception:
        logger.exception("Jeton Reddit")
    base = "https://oauth.reddit.com" if tok else "https://www.reddit.com"
    url = base + path + ("&" if "?" in path else "?") + "raw_json=1"
    h = {"User-Agent": UA}
    if tok:
        h["Authorization"] = "Bearer " + tok
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=15) as r:
        return json.loads(r.read().decode())


def refresh_subreddit(c):
    """Membres + regles d'un subreddit -> fiche Communaute."""
    sub = c.subreddit
    if not sub:
        return False
    about = (_get("/r/%s/about.json" % sub) or {}).get("data") or {}
    c.members = about.get("subscribers") or c.members
    c.members_checked_at = timezone.now()
    try:
        rules = (_get("/r/%s/about/rules.json" % sub) or {}).get("rules") or []
    except Exception:
        rules = []
    if rules:
        txt = "\n".join("• %s%s" % (r.get("short_name", ""), (" : " + r["description"].strip().replace("\n", " ")[:300])
                                     if r.get("description") else "") for r in rules)
        c.rules = txt[:4000]
        low = txt.lower()
        if c.promo == "unknown":
            if re.search(r"no (self[- ]?promo|advertis|spam|selling|promotion)|self[- ]?promotion is not|"
                         r"pas de (pub|promotion)|keine werbung", low):
                c.promo = "forbidden"
            elif re.search(r"self[- ]?promo(tion)? (saturday|sunday|thread|day)|10%|9:1|weekly .*promo", low):
                c.promo = "limited"
    c.save()
    return True


def _post_id(url):
    m = re.search(r"/comments/([a-z0-9]+)", url or "")
    return m.group(1) if m else ""


def refresh_post(o):
    """Score + nombre de commentaires d'une publication Reddit."""
    pid = _post_id(o.posted_url)
    if not pid:
        return False
    data = _get("/by_id/t3_%s.json" % pid)
    children = ((data or {}).get("data") or {}).get("children") or []
    if not children:
        return False
    d = children[0].get("data") or {}
    o.score = d.get("score")
    o.comments = d.get("num_comments")
    o.removed = bool(d.get("removed_by_category"))
    o.last_checked = timezone.now()
    o.save(update_fields=["score", "comments", "removed", "last_checked"])
    return True


def submit_url(c, title, body, link):
    """Lien Reddit « nouvelle publication » pre-rempli (publication faite par l'humain, compte de marque)."""
    sub = c.subreddit if c else ""
    if not sub:
        return ""
    q = {"title": title[:300], "text": (body + ("\n\n" + link if link and link not in body else ""))[:30000]}
    return "https://www.reddit.com/r/%s/submit?%s" % (sub, urllib.parse.urlencode(q))


# ------------------------------------------------------------------ annuaire de depart
SEED = [
    # (plateforme, nom, url, region, langue, membres estimes, promo, angle)
    ("reddit", "r/paintbynumbers", "https://www.reddit.com/r/paintbynumbers/", "global", "en", 31000, "unknown",
     "Cœur de cible. Partager des toiles terminées (flair « Finished Piece! »), répondre aux questions « kit à partir "
     "d'une photo ? » en se présentant comme fondateur. Pas de pub directe."),
    ("reddit", "r/diamondpainting", "https://www.reddit.com/r/diamondpainting/", "global", "en", 272000, "unknown",
     "Public voisin (loisir créatif relaxant). Participer aux discussions ; mentionner PaintIt seulement si pertinent."),
    ("reddit", "r/somethingimade", "https://www.reddit.com/r/somethingimade/", "global", "en", 3100000, "limited",
     "Uniquement des créations faites main par l'auteur (pas de produit commercial) : publier une toile peinte soi-même, "
     "sans lien promo dans le titre."),
    ("reddit", "r/crafts", "https://www.reddit.com/r/crafts/", "global", "en", None, "unknown",
     "Montrer le résultat peint, process avant/après. Vérifier les règles d'autopromotion avant tout lien."),
    ("reddit", "r/GiftIdeas", "https://www.reddit.com/r/GiftIdeas/", "global", "en", None, "unknown",
     "Répondre aux demandes de cadeau personnalisé (anniversaire, fête des mères…) avec transparence (« je suis le fondateur »)."),
    ("reddit", "r/acrylicpainting", "https://www.reddit.com/r/acrylicpainting/", "global", "en", None, "unknown",
     "Conseils techniques peinture acrylique ; mettre en avant la qualité des pots et de la toile."),
    ("launch", "Product Hunt", "https://www.producthunt.com/", "global", "en", None, "allowed",
     "Lancement officiel de PaintIt (outil en ligne gratuit + kit). Totalement transparent par nature."),
    ("social", "Instagram #paintbynumbers", "https://www.instagram.com/explore/tags/paintbynumbers/", "global", "en", None,
     "allowed", "Compte de marque : avant/après, timelapse de peinture, toiles de clients (avec leur accord)."),
    ("social", "TikTok #paintbynumbers", "https://www.tiktok.com/tag/paintbynumbers", "global", "en", None, "allowed",
     "Vidéos courtes : photo → toile → peinture terminée. Le format qui convertit le mieux sur ce loisir."),
    ("social", "Pinterest « paint by numbers custom »", "https://www.pinterest.com/search/pins/?q=custom%20paint%20by%20numbers",
     "global", "en", None, "allowed", "Épingles produit avec lien vers /create/ : trafic durable (SEO Pinterest)."),
    ("facebook", "Groupes FB « peinture par numéros » (FR)", "https://www.facebook.com/search/groups/?q=peinture%20par%20num%C3%A9ros",
     "fr", "fr", None, "unknown", "Recherche à faire : rejoindre 3-5 groupes actifs, lire les règles, participer avant de mentionner PaintIt."),
    ("facebook", "Gruppen „Malen nach Zahlen“ (DE)", "https://www.facebook.com/search/groups/?q=malen%20nach%20zahlen",
     "de", "de", None, "unknown", "Idem en allemand."),
    ("facebook", "Grupos „pintar por números“ (ES)", "https://www.facebook.com/search/groups/?q=pintar%20por%20n%C3%BAmeros",
     "es", "es", None, "unknown", "Idem en espagnol."),
    ("facebook", "Gruppi „dipingere con i numeri“ (IT)", "https://www.facebook.com/search/groups/?q=dipingere%20con%20i%20numeri",
     "it", "it", None, "unknown", "Idem en italien."),
    ("facebook", "Paint by numbers groups (Australia)", "https://www.facebook.com/search/groups/?q=paint%20by%20numbers%20australia",
     "au", "en", None, "unknown", "Groupes australiens (souvent liés aux craft stores) : vérifier l'autorisation des vendeurs."),
    ("facebook", "Paint by numbers groups (US)", "https://www.facebook.com/search/groups/?q=paint%20by%20numbers%20adults",
     "us", "en", None, "unknown", "Grands groupes US de loisir créatif adulte."),
    ("discord", "Serveurs Discord peinture", "https://disboard.org/servers/tag/painting", "global", "en", None, "unknown",
     "Serveurs art/peinture : canal #self-promo quand il existe, sinon participation simple."),
]


def seed():
    from .models import Community
    n = 0
    for plat, name, url, region, lang, members, promo, angle in SEED:
        if not angle:
            continue
        _obj, created = Community.objects.get_or_create(url=url, defaults=dict(
            platform=plat, name=name, region=region, lang=lang, members=members, promo=promo, angle=angle))
        n += created
    return n
