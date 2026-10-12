"""Croissance : campagnes = ciblage + contenus + diffusion + retombees.

Un clic (« Generer les actions ») : pour chaque communaute ciblee, choisit le meilleur modele de la bibliotheque
(langue, canal, theme, jamais deja utilise la-bas), le personnalise ({community}, {campaign}, {code}, {discount},
{link}) et cree une publication prete a diffuser avec SON lien traque. Les ventes sont attribuees au dernier clic
(publications : ad_ref « c:<code> » ; pubs A/B : ad_ref = jeton de la pub).
"""
from collections import OrderedDict

from django.conf import settings
from django.db.models import Count, Sum

# Preregles : une campagne prete a generer en un clic (ciblage + themes)
PRESETS = [
    {"key": "mothers", "name": "Fête des mères", "desc": "Cadeau qu'elle peint elle-même · FR, DE, ES, IT · Facebook & réseaux",
     "regions": "fr,de,es,it", "langs": "fr,de,es,it", "platforms": "facebook,social", "categories": "season,gift,story"},
    {"key": "xmas", "name": "Noël", "desc": "Cadeau photo personnalisé · toutes zones · Facebook, réseaux, Reddit",
     "regions": "", "langs": "", "platforms": "facebook,social,reddit", "categories": "season,gift"},
    {"key": "launch", "name": "Lancement produit", "desc": "Product Hunt, Hacker News, r/SideProject · angle technique",
     "regions": "global", "langs": "en", "platforms": "launch,reddit", "categories": "launch,tech"},
    {"key": "pbn", "name": "Communautés PBN (US · AU)", "desc": "Passionnés de peinture par numéros · histoires & avant/après",
     "regions": "global,us,au", "langs": "en", "platforms": "reddit,facebook", "categories": "story,beforeafter,engage,tips"},
    {"key": "seniors", "name": "Seniors & EHPAD", "desc": "Ateliers en résidence, 12 couleurs · partenariats",
     "regions": "fr", "langs": "fr", "platforms": "facebook,forum", "categories": "b2b,family"},
    {"key": "valentine", "name": "Saint-Valentin", "desc": "Peindre sa photo de couple à deux · réseaux sociaux",
     "regions": "", "langs": "", "platforms": "social", "categories": "season,gift"},
]


def apply_preset(c, key):
    p = next((x for x in PRESETS if x["key"] == key), None)
    if p:
        for f in ("regions", "langs", "platforms", "categories"):
            setattr(c, f, p[f])
    return c


# canal de la communaute -> etiquettes de canal acceptees dans les modeles
PLATFORM_TAGS = {"reddit": {"reddit"}, "facebook": {"facebook"}, "forum": {"forum"}, "discord": {"discord", "forum"},
                 "social": {"social"}, "launch": {"launch"}, "other": {"forum", "social"}}


def match_communities(c):
    from .models import Community
    qs = Community.objects.filter(active=True, priority__lte=c.min_priority or 3)
    if c.region_list():
        qs = qs.filter(region__in=c.region_list())
    if c.lang_list():
        qs = qs.filter(lang__in=c.lang_list())
    if c.platform_list():
        qs = qs.filter(platform__in=c.platform_list())
    if c.exclude_forbidden:
        qs = qs.exclude(promo="forbidden")
    return list(qs.order_by("priority", "-members"))


def pick_template(c, community, used_ids):
    """Meilleur modele pour cette communaute : langue + canal (+ theme de la campagne), en evitant les doublons."""
    from .models import PostTemplate
    tags = PLATFORM_TAGS.get(community.platform, {community.platform})
    cands = [t for t in PostTemplate.objects.filter(active=True)
             if tags & set(t.platform_list()) and t.lang == community.lang]
    if not cands and community.lang != "en":   # repli anglais si aucun modele dans la langue
        cands = [t for t in PostTemplate.objects.filter(active=True, lang="en") if tags & set(t.platform_list())]
    if c.category_list():   # theme de campagne strict : jamais de contenu hors sujet
        cands = [t for t in cands if t.category in c.category_list()]
    fresh = [t for t in cands if t.pk not in used_ids]
    pool = fresh or cands
    return pool[0] if pool else None


def personalize(text, ctx):
    for k, v in ctx.items():
        text = text.replace("{%s}" % k, v)
    return text


def generate_actions(c):
    """Cree les publications ciblees de la campagne (idempotent : une seule par communaute et par modele)."""
    from .models import Outreach
    from . import community as cm
    created, skipped = [], []
    for com in match_communities(c):
        if Outreach.objects.filter(campaign=c, community=com).exists():
            skipped.append("%s (déjà prévu)" % com.name)
            continue
        used = set(Outreach.objects.filter(community=com).exclude(template=None).values_list("template_id", flat=True))
        t = pick_template(c, com, used)
        if not t:
            skipped.append("%s (aucun modèle pour ce thème/canal/langue)" % com.name)
            continue
        if Outreach.objects.filter(campaign=c, community=com, template=t).exists():
            skipped.append("%s (déjà prévu)" % com.name)
            continue
        o = Outreach.objects.create(community=com, template=t, campaign=c, code=cm.new_code(), title=t.hook, body=t.body,
                                    target_url=settings.SITE_URL + (c.target_path or t.target_path or "/create/"))
        d = c.discount
        ctx = {"link": cm.tracked_url(o), "community": com.name, "campaign": c.name,
               "code": d.code if d else "", "discount": ("-%d %%" % d.percent) if d else ""}
        body = personalize(o.body, ctx)
        if c.append_code and d and d.code not in body:
            body += "\n\n" + {"fr": "Code %s : -%d %% pour la communauté %s.", "de": "Code %s: -%d %% für die Community %s.",
                              "es": "Código %s: -%d %% para la comunidad %s.", "it": "Codice %s: -%d %% per la community %s."
                              }.get(com.lang, "Code %s: -%d%% for the %s community.") % (d.code, d.percent, com.name)
        o.title, o.body = personalize(o.title, ctx), body
        o.save(update_fields=["title", "body"])
        created.append(o)
    return created, skipped


def stats(c):
    """Retombees d'une campagne : publications + pubs A/B, par canal, avec ventes et CA attribues."""
    from .models import MarketingAd, Order, Outreach
    paid = Order.objects.filter(status__in=Order.PAID_STATUSES).exclude(ad_ref="")
    conv = {r["ad_ref"]: r for r in paid.values("ad_ref").annotate(n=Count("id"), ca=Sum("total"))}
    posts = list(Outreach.objects.filter(campaign=c).select_related("community", "template"))
    ads = list(MarketingAd.objects.filter(campaign=c.name))
    channels = OrderedDict()
    tot = {"actions": len(posts), "posted": 0, "clicks": 0, "orders": 0, "revenue": 0.0, "replies": 0,
           "ads": len(ads), "ad_views": 0}
    for o in posts:
        r = conv.get("c:" + o.code) or {}
        o.orders, o.revenue = r.get("n", 0), round(r.get("ca") or 0, 2)
        key = o.community.get_platform_display() if o.community else "Autre"
        ch = channels.setdefault(key, {"actions": 0, "posted": 0, "clicks": 0, "orders": 0, "revenue": 0.0})
        ch["actions"] += 1; ch["posted"] += bool(o.posted_url); ch["clicks"] += o.unique_clicks
        ch["orders"] += o.orders; ch["revenue"] += o.revenue
        tot["posted"] += bool(o.posted_url); tot["clicks"] += o.unique_clicks
        tot["orders"] += o.orders; tot["revenue"] += o.revenue; tot["replies"] += o.new_replies
    for a in ads:
        r = conv.get(a.token) or {}
        a.orders, a.revenue = r.get("n", 0), round(r.get("ca") or 0, 2)
        ch = channels.setdefault("Pubs A/B", {"actions": 0, "posted": 0, "clicks": 0, "orders": 0, "revenue": 0.0})
        ch["actions"] += 1; ch["posted"] += 1; ch["clicks"] += a.unique_clicks
        ch["orders"] += a.orders; ch["revenue"] += a.revenue
        tot["clicks"] += a.unique_clicks; tot["orders"] += a.orders; tot["revenue"] += a.revenue; tot["ad_views"] += a.views
    if c.discount:
        tot["code_uses"] = c.discount.used_count
    tot["revenue"] = round(tot["revenue"], 2)
    tot["conv"] = round(tot["orders"] / tot["clicks"] * 100, 1) if tot["clicks"] else None
    tot["roi"] = round((tot["revenue"] - c.budget) / c.budget * 100) if c.budget else None
    for ch in channels.values():
        ch["revenue"] = round(ch["revenue"], 2)
    return {"posts": posts, "ads": ads, "channels": channels, "tot": tot}
