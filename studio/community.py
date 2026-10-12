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


def _slug_code(name):
    import re as _re, unicodedata
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().upper()
    return _re.sub(r"[^A-Z0-9]+", "", t)[:12] or "PAINTIT"


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
    if c and "news.ycombinator.com" in (c.url or ""):
        return "https://news.ycombinator.com/submitlink?" + urllib.parse.urlencode({"u": link, "t": title[:80]})
    sub = c.subreddit if c else ""
    if not sub:
        return ""
    q = {"title": title[:300], "text": (body + ("\n\n" + link if link and link not in body else ""))[:30000]}
    return "https://www.reddit.com/r/%s/submit?%s" % (sub, urllib.parse.urlencode(q))


# ------------------------------------------------------------------ audit des canaux (pre-remplissage)
# Chaque entree : plateforme, nom, url, region, langue, membres estimes (None = a actualiser), autopromo, priorite,
# angle (audit : public, pourquoi, comment), notes (format conseille, moment, precautions).
# « unknown » = regles non verifiees : bouton « Actualiser » (Reddit) ou lecture des regles avant toute publication.
R = "https://www.reddit.com/r/%s/"
AUDIT = [
    # ---- Reddit, coeur de cible
    dict(platform="reddit", name="r/paintbynumbers", url=R % "paintbynumbers", region="global", lang="en", members=31000,
         promo="unknown", priority=1,
         angle="Cœur de cible : passionnés de PBN, majoritairement US/UK/AU. Publications « Finished Piece! » très suivies. "
               "Angle : partager une toile PaintIt réellement peinte (avant/après photo → toile), raconter l'histoire, "
               "répondre aux questions techniques (nombre de couleurs, taille des zones). Se présenter comme fondateur.",
         notes="Format : photo de la toile terminée + photo d'origine en 2e image. Pas de lien dans le titre ; lien en commentaire "
               "seulement si on le demande. Meilleur moment : week-end, 15-18 h (heure US Est)."),
    dict(platform="reddit", name="r/diamondpainting", url=R % "diamondpainting", region="global", lang="en", members=272000,
         promo="unknown", priority=2,
         angle="Public voisin et très large (loisir créatif relaxant, kits personnalisés déjà connus). Angle : « j'ai testé la "
               "peinture par numéros personnalisée après des années de diamond painting », comparaison honnête.",
         notes="Ne pas publier de lien commercial sans y être invité. Participer d'abord aux fils d'entraide."),
    dict(platform="reddit", name="r/somethingimade", url=R % "somethingimade", region="global", lang="en", members=3100000,
         promo="limited", priority=2,
         angle="Énorme audience « fait main ». Seules les créations faites par l'auteur sont acceptées (pas de produit "
               "commercial) : publier une toile que TU as peinte, l'histoire derrière. Aucune mention commerciale dans le titre.",
         notes="Si on demande d'où vient le kit : répondre en transparence (« je suis le fondateur, c'est notre outil »)."),
    dict(platform="reddit", name="r/crafts", url=R % "crafts", region="global", lang="en", members=None, promo="unknown",
         priority=2, angle="Loisirs créatifs généralistes. Angle : processus (photo → numérotation → peinture), conseils pour débutants.",
         notes="Lire la règle d'autopromotion (souvent fil dédié). Photos de process > produit fini seul."),
    dict(platform="reddit", name="r/acrylicpainting", url=R % "acrylicpainting", region="global", lang="en", members=None,
         promo="unknown", priority=3,
         angle="Peintres acrylique. Angle technique : qualité des pots, mélange, finitions sur une toile numérotée.",
         notes="Communauté exigeante : contenu pédagogique uniquement."),
    dict(platform="reddit", name="r/painting", url=R % "painting", region="global", lang="en", members=None, promo="unknown",
         priority=3, angle="Peinture généraliste. Partager une toile terminée de qualité, discussion sur le PBN comme porte d'entrée.",
         notes="Le PBN peut être mal vu par certains peintres : rester humble, mettre en avant l'apprentissage."),
    # ---- Reddit, cas d'usage cadeau / proches
    dict(platform="reddit", name="r/GiftIdeas", url=R % "GiftIdeas", region="global", lang="en", members=None, promo="unknown",
         priority=1,
         angle="Les demandes « cadeau personnalisé / pour maman / anniversaire de couple » sont exactement notre cas d'usage. "
               "Angle : RÉPONDRE aux demandes (pas publier) avec une suggestion utile et une transparence totale.",
         notes="Activer une alerte sur « personalized gift », « photo gift », « gift for mom ». Réponse type dans les brouillons."),
    dict(platform="reddit", name="r/weddingplanning", url=R % "weddingplanning", region="global", lang="en", members=None,
         promo="unknown", priority=3,
         angle="Cadeaux de mariage / témoins / anniversaire de mariage à partir d'une photo du couple. Répondre aux demandes de cadeaux.",
         notes="Pas de publication promotionnelle : réponses uniquement."),
    dict(platform="reddit", name="r/AgingParents", url=R % "AgingParents", region="global", lang="en", members=None,
         promo="forbidden", priority=3,
         angle="Familles cherchant des activités pour un parent âgé. Le PBN 12 couleurs est une vraie réponse (motricité fine, "
               "souvenir de famille). Répondre seulement si on demande des idées d'activité, sans lien sauf demande.",
         notes="Communauté sensible : ton bienveillant, jamais d'argument commercial. Considérer comme « autopromo interdite »."),
    # ---- Reddit, lancement / produit
    dict(platform="reddit", name="r/SideProject", url=R % "SideProject", region="global", lang="en", members=None,
         promo="allowed", priority=1,
         angle="Les créateurs y présentent leurs projets : présenter PaintIt (algorithme photo → toile numérotée, jeu en ligne "
               "gratuit). Angle « comment j'ai construit l'algorithme » + retours demandés.",
         notes="Format : texte + GIF de la génération. Répondre à tous les commentaires dans les 2 premières heures."),
    dict(platform="reddit", name="r/InternetIsBeautiful", url=R % "InternetIsBeautiful", region="global", lang="en", members=None,
         promo="limited", priority=2,
         angle="Sites web gratuits et utiles. Le générateur + jeu en ligne gratuit peuvent y avoir leur place si l'usage gratuit "
               "est réel et sans inscription. Lien vers /create/ (aperçu gratuit) ou /play/.",
         notes="Règles strictes sur les sites commerciaux : vérifier avant, insister sur la partie gratuite."),
    dict(platform="reddit", name="r/EntrepreneurRideAlong", url=R % "EntrepreneurRideAlong", region="global", lang="en",
         members=None, promo="limited", priority=3,
         angle="Retour d'expérience d'entrepreneur (fournisseur, marges, premiers clients). Visibilité indirecte et contacts.",
         notes="Contenu transparent, chiffres réels ; pas de lien dans le corps sauf autorisé."),
    # ---- Lancement / tech
    dict(platform="launch", name="Product Hunt", url="https://www.producthunt.com/", region="global", lang="en", members=None,
         promo="allowed", priority=1,
         angle="Lancement officiel : « Turn any photo into a paint-by-numbers canvas, try it free online ». Préparer visuels, "
               "GIF, premier commentaire du fondateur.",
         notes="Lancer un mardi/mercredi 00:01 heure Pacifique. Prévenir sa communauté la veille (sans demander de votes)."),
    dict(platform="launch", name="Hacker News (Show HN)", url="https://news.ycombinator.com/submit", region="global", lang="en",
         members=None, promo="allowed", priority=2,
         angle="Angle technique : quantification couleur CIEDE2000, géométrie des zones partagées, palette fournisseur. "
               "Show HN accepte un produit essayable sans inscription.",
         notes="Titre sobre « Show HN: … ». Être présent pour répondre pendant 3 h."),
    dict(platform="launch", name="Indie Hackers", url="https://www.indiehackers.com/", region="global", lang="en", members=None,
         promo="allowed", priority=3, angle="Récit de construction + chiffres. Contacts, retours, backlinks.", notes=""),
    # ---- Réseaux sociaux (compte de marque)
    dict(platform="social", name="TikTok #paintbynumbers", url="https://www.tiktok.com/tag/paintbynumbers", region="global",
         lang="en", members=None, promo="allowed", priority=1,
         angle="Le canal n°1 de ce loisir : vidéos « photo → toile → peinture terminée », timelapses relaxants, réactions de "
               "personnes qui reçoivent leur portrait en cadeau.",
         notes="3 vidéos/semaine, 15-30 s, son tendance. Variantes FR/DE/ES/IT avec sous-titres. Lien en bio vers /go/…"),
    dict(platform="social", name="Instagram #paintbynumbers", url="https://www.instagram.com/explore/tags/paintbynumbers/",
         region="global", lang="en", members=None, promo="allowed", priority=1,
         angle="Reels avant/après, carrousels « 12 vs 24 vs 36 couleurs », repartage des toiles clients (avec accord).",
         notes="Hashtags : #paintbynumbers #custompaintbynumbers #peintureparnumero #malennachzahlen #pinturapornumeros."),
    dict(platform="social", name="Pinterest (paint by numbers custom)", url="https://www.pinterest.com/search/pins/?q=custom%20paint%20by%20numbers",
         region="global", lang="en", members=None, promo="allowed", priority=1,
         angle="Moteur de recherche visuel à trafic durable, très fort sur « idée cadeau ». Épingles avant/après en 2:3 avec lien "
               "traqué vers /create/. Tableaux par langue.",
         notes="10 épingles/semaine, titres SEO (« Cadeau personnalisé photo », « Personalized gift from photo »)."),
    dict(platform="social", name="YouTube Shorts", url="https://www.youtube.com/results?search_query=paint+by+numbers+custom",
         region="global", lang="en", members=None, promo="allowed", priority=2,
         angle="Mêmes vidéos que TikTok, référencement Google long terme.", notes="Recycler les TikTok."),
    # ---- Q&A
    dict(platform="forum", name="Quora (EN)", url="https://www.quora.com/search?q=custom%20paint%20by%20numbers", region="global",
         lang="en", members=None, promo="limited", priority=2,
         angle="Questions « best custom paint by numbers », « personalized gift ideas from photo » : réponses détaillées, "
               "divulgation « I'm the founder of PaintIt ».",
         notes="Réponses longues et utiles ; les réponses Quora remontent sur Google."),
    dict(platform="forum", name="gutefrage.net (DE)", url="https://www.gutefrage.net/suche?query=malen%20nach%20zahlen%20eigenes%20foto",
         region="de", lang="de", members=None, promo="limited", priority=2,
         angle="Plus grande plateforme de Q&A germanophone. Questions « Malen nach Zahlen vom eigenen Foto ? », « Geschenkidee ». "
               "Répondre en allemand, en transparence.",
         notes="Autopromotion encadrée : réponse utile d'abord, lien seulement si pertinent."),
    dict(platform="forum", name="Doctissimo forums (FR)", url="https://forum.doctissimo.fr/", region="fr", lang="fr", members=None,
         promo="unknown", priority=3,
         angle="Grands forums FR (famille, loisirs, idées cadeaux). Répondre aux demandes de cadeaux personnalisés / activités seniors.",
         notes="Vérifier la charte (publicité généralement interdite) : réponses sans lien sauf demande."),
    dict(platform="forum", name="urbia.de (DE)", url="https://www.urbia.de/forum", region="de", lang="de", members=None,
         promo="unknown", priority=3, angle="Communauté familles DE : activités enfants dès 4 ans (12 couleurs), cadeaux grands-parents.",
         notes="Charte à vérifier."),
    # ---- Facebook (recherches pré-réglées)
    dict(platform="facebook", name="Groupes FB peinture par numéros (FR)",
         url="https://www.facebook.com/search/groups/?q=peinture%20par%20num%C3%A9ros", region="fr", lang="fr", members=None,
         promo="unknown", priority=1,
         angle="Les groupes FB sont le principal lieu d'échange francophone sur le PBN (pas de forum dédié trouvé). Rejoindre 3-5 "
               "groupes actifs, partager ses toiles, répondre aux questions, publier seulement les jours autorisés.",
         notes="Utiliser la page PaintIt comme identité (fonction « interagir en tant que page ») quand le groupe l'autorise."),
    dict(platform="facebook", name="Gruppen Malen nach Zahlen (DE/AT)",
         url="https://www.facebook.com/search/groups/?q=malen%20nach%20zahlen", region="de", lang="de", members=None,
         promo="unknown", priority=1, angle="Idem en allemand (marché DE/AT large sur ce loisir).", notes=""),
    dict(platform="facebook", name="Grupos pintar por números (ES)",
         url="https://www.facebook.com/search/groups/?q=pintar%20por%20n%C3%BAmeros", region="es", lang="es", members=None,
         promo="unknown", priority=2, angle="Idem en espagnol.", notes=""),
    dict(platform="facebook", name="Gruppi dipingere con i numeri (IT)",
         url="https://www.facebook.com/search/groups/?q=dipingere%20con%20i%20numeri", region="it", lang="it", members=None,
         promo="unknown", priority=2, angle="Idem en italien.", notes=""),
    dict(platform="facebook", name="Paint by numbers groups (US)",
         url="https://www.facebook.com/search/groups/?q=paint%20by%20numbers%20adults", region="us", lang="en", members=None,
         promo="unknown", priority=1, angle="Grands groupes US de PBN adultes : publications « finished piece », entraide.",
         notes="Beaucoup de groupes sont tenus par des marques concurrentes : y participer sans promotion."),
    dict(platform="facebook", name="Paint by numbers groups (Australia)",
         url="https://www.facebook.com/search/groups/?q=paint%20by%20numbers%20australia", region="au", lang="en", members=None,
         promo="unknown", priority=2,
         angle="Pas de groupe PBN australien majeur identifié : viser les groupes de loisirs créatifs / painted rocks (ex. groupes "
               "régionaux « Rocks » de plusieurs dizaines de milliers de membres) avec du contenu de création.",
         notes="Livraison AU : préciser délais et frais avant de publier."),
    dict(platform="facebook", name="Groupes animateurs EHPAD / seniors (FR)",
         url="https://www.facebook.com/search/groups/?q=animation%20ehpad", region="fr", lang="fr", members=None,
         promo="unknown", priority=2,
         angle="Débouché B2B : ateliers peinture en résidences seniors (12 couleurs, photo de famille ou du village). "
               "Proposer un kit test aux animateurs, retours terrain.",
         notes="Approche partenariat, pas publicité. Prévoir une offre établissement."),
    # ---- Discord
    dict(platform="discord", name="Serveurs Discord peinture / art", url="https://disboard.org/servers/tag/painting",
         region="global", lang="en", members=None, promo="unknown", priority=3,
         angle="Serveurs art/peinture : utiliser le canal #self-promo quand il existe, sinon simple participation.",
         notes="Faible conversion, utile pour les retours produit."),
    # ---- Avis / confiance (vrais clients uniquement)
    dict(platform="other", name="Trustpilot (profil entreprise)", url="https://business.trustpilot.com/", region="global",
         lang="en", members=None, promo="allowed", priority=2,
         angle="Créer le profil, inviter UNIQUEMENT les vrais clients après livraison (e-mail d'avis déjà envoyé par PaintIt).",
         notes="Jamais d'avis rédigés par soi : interdit (UE, FTC, ACL) et détecté."),
    dict(platform="other", name="ProductReview.com.au", url="https://www.productreview.com.au/", region="au", lang="en",
         members=None, promo="allowed", priority=3, angle="Référence des avis en Australie : profil + invitations clients AU.",
         notes="Uniquement des clients réels."),
]

# Brouillons de publication pre-remplis (prets a relire / personnaliser), lien traque ajoute automatiquement.
DRAFTS = [
    ("r/paintbynumbers", "I turned a photo of my grandparents into a paint by numbers, here's how it came out",
     "Finished this one over three weekends. Original photo in the 2nd picture.\n\n"
     "Full disclosure: I'm the founder of PaintIt, a small site that turns a photo into a numbered canvas. I built the "
     "algorithm because store-bought custom kits I tried had tiny unpaintable areas. This one is 24 colours, 40x50 cm.\n\n"
     "Happy to answer anything about how the zones/colours are chosen, or tips for painting faces.", "/create/"),
    ("r/GiftIdeas", "(Réponse type) Personalized gift from a photo",
     "If they like crafts, a custom paint-by-numbers from a photo that matters to them (pet, wedding, family) is a "
     "really personal gift: they paint it themselves and keep it on the wall.\n\n"
     "Disclosure: I run PaintIt, which does exactly this. You can preview the canvas from your photo for free before "
     "deciding anything; 12 colours is easy enough for kids and grandparents, 36+ for detail lovers.", "/create/"),
    ("r/SideProject", "I built an algorithm that turns any photo into a paintable paint-by-numbers canvas (free to try)",
     "Hi! Side project turned small business: PaintIt.\n\n"
     "What it does: upload a photo → it picks the palette (CIEDE2000 colour distance, snapped to real paint colours a "
     "manufacturer can mix) → builds zones big enough to actually paint → you can paint it online for free, or order the kit.\n\n"
     "Hardest part was avoiding micro-zones and keeping numbers readable. I'd love feedback on the result quality and the UX.",
     "/create/"),
    ("r/InternetIsBeautiful", "A free online tool that turns your photo into a paint-by-numbers you can paint in the browser",
     "No sign-up needed: upload a photo, get the numbered canvas and paint it online. (I'm the maker; there's an optional "
     "physical kit, but the online part is free.)", "/play/"),
    ("Product Hunt", "PaintIt — Turn any photo into a paint-by-numbers canvas",
     "Tagline : Your memory, your masterpiece. Try it free online, then paint it at home.\n\n"
     "Premier commentaire (fondateur) : pourquoi PaintIt (kits du commerce aux zones trop petites), ce qui est différent "
     "(algorithme, couleurs réellement disponibles, jeu en ligne gratuit), ce que vous aimeriez savoir.", "/create/"),
    ("Hacker News (Show HN)", "Show HN: Photo to paint-by-numbers with paintable zones and real paint colours",
     "Built this to fix two problems with custom PBN kits: unpaintable micro-areas and colours the factory can't reproduce. "
     "Pipeline: mean-shift smoothing → k-means in Lab → snap to the supplier's available paints (CIEDE2000) → zone merging "
     "with a minimum paintable size → shared-edge SVG so outlines never gap. Free to try in the browser.", "/create/"),
    ("Groupes FB peinture par numéros (FR)", "Ma toile terminée à partir d'une photo de famille",
     "Je partage ma dernière toile, peinte à partir d'une photo de mes grands-parents (photo d'origine en 2e image). "
     "En toute transparence : je suis le fondateur de PaintIt, on transforme une photo en peinture par numéros. "
     "Vos retours m'intéressent vraiment, notamment sur le nombre de couleurs que vous préférez !", "/create/"),
    ("Gruppen Malen nach Zahlen (DE/AT)", "Mein Bild nach einem eigenen Foto, fertig gemalt",
     "Ich teile mein fertiges Bild nach einem Familienfoto (Originalfoto im 2. Bild). Ganz offen: Ich bin der Gründer "
     "von PaintIt, wir machen aus Fotos Malen-nach-Zahlen-Leinwände. Mich interessiert eure Meinung, z. B. wie viele "
     "Farben ihr am liebsten habt!", "/create/"),
    ("Grupos pintar por números (ES)", "Mi cuadro terminado a partir de una foto familiar",
     "Comparto mi último cuadro, pintado a partir de una foto de mis abuelos (foto original en la 2.ª imagen). Con total "
     "transparencia: soy el fundador de PaintIt, convertimos fotos en pintura por números. ¡Me encantaría saber vuestra opinión!",
     "/create/"),
    ("Gruppi dipingere con i numeri (IT)", "Il mio quadro finito da una foto di famiglia",
     "Condivido il mio ultimo quadro, dipinto da una foto dei miei nonni (foto originale nella 2ª immagine). In tutta "
     "trasparenza: sono il fondatore di PaintIt, trasformiamo le foto in dipinti con i numeri. Mi interessa il vostro parere!",
     "/create/"),
    ("Pinterest (paint by numbers custom)", "Personalized gift from photo: paint by numbers of your pet / family",
     "Épingle 2:3 : photo d'origine en haut, toile numérotée au milieu, toile peinte en bas. Description : « Turn your "
     "favourite photo into a paint-by-numbers kit. Free online preview, delivered to your door. »", "/create/"),
    ("Groupes animateurs EHPAD / seniors (FR)", "Atelier peinture seniors : peinture par numéros à partir de photos du village",
     "Bonjour, je suis le fondateur de PaintIt. Nous créons des peintures par numéros à partir de photos (12 couleurs, "
     "grandes zones, adaptées à partir de 4 ans comme aux seniors). Je cherche 2-3 résidences pour tester gratuitement "
     "un atelier et recueillir vos retours d'animateurs. Intéressé(e) ? Écrivez-moi en privé.", "/create/"),
]


def seed():
    """Cree / met a jour l'annuaire (audit) et les brouillons de publication. -> (communautes, brouillons)."""
    from .models import Community, Outreach
    n = d = 0
    for row in AUDIT:
        row = dict(row)
        url = row.pop("url")
        obj, created = Community.objects.get_or_create(url=url, defaults=row)
        if not created:   # mise a jour de l'audit, sans ecraser ce que l'admin a verifie (membres / regles / promo)
            for k in ("name", "angle", "notes", "priority", "platform", "region", "lang"):
                setattr(obj, k, row[k])
            if obj.promo == "unknown":
                obj.promo = row["promo"]
            obj.save()
        n += created
    for cname, title, body, path in DRAFTS:
        c = Community.objects.filter(name=cname).first()
        if c and not Outreach.objects.filter(community=c, title=title).exists():
            Outreach.objects.create(community=c, code=new_code(), title=title, body=body,
                                    target_url=settings.SITE_URL + path)
            d += 1
    return n, d
