import datetime
import os
import re
import time
import logging
logger = logging.getLogger("studio.views")
import threading
import uuid

from django.conf import settings
from django.utils import timezone
from django.http import FileResponse, Http404, HttpResponse, HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_sameorigin, xframe_options_exempt
from django.utils.translation import get_language, gettext as _
from django.contrib.admin.views.decorators import staff_member_required

from . import fulfillment, supplier, discounts, address, emails, payments, receipts, jobs, genqueue, security
from .forms import UploadForm, DeliveryForm, ContactForm, FORMATS, dimensions
from .models import Order, OrderEvent, Pricing, DigitalCanvas, EmailCode, CompanyInfo
from .pipeline import generate, compute_price, price_cfg

PHASE_LABELS = {
    "read":   {"fr": "Lecture de l'image", "en": "Reading the image", "de": "Bild wird gelesen", "es": "Leyendo la imagen", "it": "Lettura dell'immagine"},
    "colors": {"fr": "Analyse des couleurs", "en": "Analyzing colors", "de": "Farbanalyse", "es": "Analizando colores", "it": "Analisi dei colori"},
    "zones":  {"fr": "Regroupement des zones", "en": "Grouping zones", "de": "Zonen werden gruppiert", "es": "Agrupando zonas", "it": "Raggruppamento delle zone"},
    "canvas": {"fr": "Trace du canvas", "en": "Drawing the canvas", "de": "Canvas wird gezeichnet", "es": "Trazando el lienzo", "it": "Tracciamento della tela"},
    "number": {"fr": "Numerotation des zones", "en": "Numbering zones", "de": "Zonen werden nummeriert", "es": "Numerando zonas", "it": "Numerazione delle zone"},
    "final":  {"fr": "Finalisation", "en": "Finishing up", "de": "Abschluss", "es": "Finalizacion", "it": "Finalizzazione"},
}


def _phase_label(key):
    lang = (get_language() or "fr")[:2]
    return PHASE_LABELS.get(key, {}).get(lang) or PHASE_LABELS.get(key, {}).get("en") or key


def _rate():
    return Pricing.get().discount_rate


def home(request):
    import json as _json
    # carrousel du home : modeles GRATUITS tires au hasard dans toute la galerie
    lib = list(DigitalCanvas.objects.filter(email="__library__", uid__startswith="gal-", price=0, hidden=False)
               .order_by("?")[:12])
    from django.templatetags.static import static as _static
    lib_slugs = {s: sl and not h for s, sl, h in DigitalCanvas.objects.filter(email="__library__").exclude(showcase_slug="")
                 .values_list("showcase_slug", "in_slider", "hidden")}
    slides = []
    # assets du home : livres avec le code (static) + ajoutes depuis l'admin (media, sans collectstatic)
    for folder, url_for in ((os.path.join(settings.BASE_DIR, "studio", "static", "studio", "showcase"),
                             lambda n: _static("studio/showcase/" + n)),
                            (os.path.join(settings.MEDIA_ROOT, "showcase"),
                             lambda n: settings.MEDIA_URL + "showcase/" + n)):
        if os.path.isdir(folder):
            for f in sorted(os.listdir(folder)):
                if f.endswith("_pbn.png") and os.path.exists(os.path.join(folder, f[:-8] + "_template.png")) \
                        and (not lib_slugs or lib_slugs.get(f[:-8], False)):   # choix « slider » du Catalogue
                    try:
                        slides.append({"p": url_for(f), "t": url_for(f[:-8] + "_template.png")})
                    except ValueError:   # absent du manifest (collectstatic pas encore passe)
                        pass
    from . import seo
    return render(request, "studio/home.html",
                  {"ld_product": seo.dump(seo.product(with_reviews=True)), "reviews": seo.reviews_summary(),
                   "colors_available": Pricing.get().available_colors(),
                   "library_preview": lib, "showcase_slides": _json.dumps(slides),
                   "first_slide": next((x for x in slides if "moto" in x["p"]), slides[0] if slides else None)})


# ---------------- Upload / preview ----------------
def _save_upload(f):
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff"):
        ext = ".jpg"   # jamais d'extension arbitraire (.html, .svg...) dans media/
    updir = os.path.join(settings.MEDIA_ROOT, "uploads")
    os.makedirs(updir, exist_ok=True)
    path = os.path.join(updir, uuid.uuid4().hex + ext)
    with open(path, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)
    if ext in (".heic", ".heif"):   # OpenCV ne lit pas le HEIC (iPhone) -> JPEG
        try:
            from PIL import Image, ImageOps
            jpg = path.rsplit(".", 1)[0] + ".jpg"
            ImageOps.exif_transpose(Image.open(path)).convert("RGB").save(jpg, "JPEG", quality=95)
            os.remove(path)
            path = jpg
        except Exception:
            logger.exception("Conversion HEIC")
    return path


def _run_generation(uid, src, colors, w, h, fmt, orientation, lang, focus=(0.5, 0.5), source_name=None,
                    level="auto"):
    def cb(pct, label):
        jobs.update(uid, pct=int(pct), label=label)

    try:
        from .forms import DENSITY_LEVELS
        dens, det = DENSITY_LEVELS.get(level, DENSITY_LEVELS["auto"])
        result = generate(src, colors, w, h, uid=uid, progress=cb, focus=focus, source_name=source_name,
                          density=dens, detail=det)
        canvas_price = compute_price(fmt, colors)
        order = {
            **result, "colors": colors, "width_cm": w, "height_cm": h, "gen_w": w, "gen_h": h, "focus": list(focus),
            "orientation": orientation, "format_label": "%g x %g cm" % (w, h),
            "difficulty": level, "lang": lang,
            "canvas_price": canvas_price, "brushes": False, "brushes_amount": 0.0,
            "price": canvas_price,
        }
        jobs.update(uid, pct=100, label="final", done=True, order=order)
    except Exception as exc:                       # pragma: no cover
        jobs.update(uid, done=True, error=str(exc))
    finally:
        # Thread hors cycle requete : on ferme les connexions DB ouvertes ici.
        from django.db import connections
        connections.close_all()


def upload(request):
    order = request.session.get("order")
    last_photo = request.session.get("last_photo")
    if request.method == "POST":
        form = UploadForm(request.POST, request.FILES)
        if form.is_valid() and security.rate_limited("gen:" + security.client_ip(request), 40, 3600):
            form.add_error(None, _("Trop de générations en peu de temps. Réessayez dans quelques minutes."))
        elif form.is_valid():
            f = form.cleaned_data.get("photo")
            if f:
                src = _save_upload(f)
                request.session["last_photo"] = src
                request.session["last_photo_name"] = f.name
            else:
                src = last_photo
            if not src or not os.path.exists(src):
                form.add_error("photo", "Merci d'ajouter une photo.")
            else:
                p = Pricing.get()
                fmt = "40x50" if p.format_available("40x50") else p.available_formats()[0]
                colors = int(form.cleaned_data["colors"])
                level = form.cleaned_data.get("density") or "auto"
                orientation = form.cleaned_data["orientation"]
                w, h = dimensions(fmt, orientation)
                try:
                    fx = float(request.POST.get("focus_x", "0.5"))
                    fy = float(request.POST.get("focus_y", "0.5"))
                except (TypeError, ValueError):
                    fx, fy = 0.5, 0.5
                request.session["last_focus"] = [fx, fy]      # retour depuis l'apercu : meme cadrage
                uid = f"{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:4].upper()}"
                if request.POST.get("trial"):
                    request.session["digital_trial"] = True    # « Ajouter · 0,99 € » depuis Mes toiles
                else:
                    request.session.pop("digital_trial", None)
                jobs.start(uid)
                sname = request.session.get("last_photo_name") or os.path.basename(src)
                try:
                    genqueue.submit(_run_generation, uid, src, colors, w, h, fmt,
                                    orientation, get_language() or "fr", (fx, fy),
                                    source_name=sname, level=level)
                except genqueue.QueueFull:
                    jobs.pop(uid)
                    form.add_error(None, _("Nos serveurs sont très sollicités en ce moment. "
                                           "Merci de réessayer dans quelques secondes."))
                else:
                    from . import analytics     # comptee seulement si la generation est vraiment lancee
                    analytics.log_event(request, "generation", "Aperçu (upload)", "/create/")
                    return redirect("%s?uid=%s" % (reverse("studio:processing"), uid))
    else:
        initial = {}
        if order:
            initial = {"orientation": order.get("orientation"),
                       "colors": str(order.get("colors")),
                       "density": order.get("difficulty") if order.get("difficulty") in ("easy", "medium", "expert") else "auto"}
        form = UploadForm(initial=initial)
    has_photo = bool(last_photo and os.path.exists(last_photo))
    fx, fy = (request.session.get("last_focus") or [0.5, 0.5]) if has_photo else (0.5, 0.5)
    # adresse propre a CETTE photo : le navigateur ne peut pas reafficher une ancienne photo mise en cache
    import hashlib
    photo_v = hashlib.sha1(("%s:%s" % (last_photo, os.path.getmtime(last_photo))).encode()).hexdigest()[:12] \
        if has_photo else ""
    from . import seo
    resp = render(request, "studio/upload.html", {
        "ld_product": seo.dump(seo.product()),
        "form": form, "price_cfg": price_cfg(), "has_photo": has_photo, "photo_v": photo_v,
        "focus_x": "%.4f" % fx, "focus_y": "%.4f" % fy,
    })
    resp["Cache-Control"] = "no-store, private"                # retour arriere : page rechargee avec la photo en cours
    return resp


def processing(request):
    uid = request.GET.get("uid")
    if not uid or jobs.get(uid) is None:
        return redirect("studio:upload")
    return render(request, "studio/processing.html", {"uid": uid})


def gen_progress(request):
    uid = request.GET.get("uid")
    job = jobs.get(uid)
    if not job:
        return JsonResponse({"error": "unknown", "done": True}, status=404)
    return JsonResponse({"pct": job["pct"], "label": _phase_label(job["label"]),
                         "done": job["done"], "error": job["error"]})


def gen_finalize(request):
    uid = request.GET.get("uid")
    job = jobs.get(uid)
    if not job or not job.get("done"):
        return redirect("studio:upload")
    if job.get("error") or not job.get("order"):
        jobs.pop(uid)
        return redirect("studio:upload")
    request.session["order"] = job["order"]
    jobs.pop(uid)
    o = job["order"]
    verified = request.session.get("verified_email", "")

    def _save_digital():
        try:
            DigitalCanvas.objects.update_or_create(
                email=verified, uid=uid,
                defaults=dict(colors=o["colors"], orientation=o["orientation"],
                              width_cm=o["width_cm"], height_cm=o["height_cm"], source="digital"))
        except Exception:
            pass

    def _track():
        u = request.session.get("my_uids", []);
        if uid not in u: u.append(uid); request.session["my_uids"] = u[-60:]
    if request.session.pop("digital_mode", False):
        request.session.pop("digital_trial", None)
        _save_digital(); _track()
        request.session["free_used"] = True
        return redirect("studio:digipaint", uid=uid)   # toile numerique -> le jeu
    if request.session.pop("digital_trial", False):
        request.session["digital_trial_uid"] = uid      # essai payant : jeu direct, sauvegarde a 0,99 EUR
        return redirect("studio:digipaint", uid=uid)

    # Flux de creation classique : la 1re toile est aussi enregistree comme
    # toile numerique GRATUITE de l'utilisateur (nouvel utilisateur).
    already = (DigitalCanvas.objects.filter(email=verified).exists() if verified
               else bool(request.session.get("free_used")))
    if not already:
        _save_digital(); _track()
        request.session["free_used"] = True
    request.session.pop("buy_mode", None)
    return redirect("studio:preview")            # revelation : on voit l'apercu, clic -> jeu


def digipaint_buy(request, uid):
    """Retour a la commande (kit vide ou version peinte) SANS regenerer."""
    request.session["order_variant"] = request.GET.get("v", "blank")
    import json
    import re
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    if not os.path.exists(os.path.join(d, f"{uid}_digipaint.svg")):
        return redirect("studio:upload")
    # « Modifier » (couleurs, cadrage) depuis l'apercu doit repartir de la photo de CETTE toile,
    # pas de la derniere photo envoyee sur le site
    from .pipeline import source_file
    src = source_file(d, uid)
    if src and os.path.exists(src):
        request.session["last_photo"] = src
        request.session["last_photo_name"] = os.path.basename(src)
    else:
        request.session.pop("last_photo", None)
        request.session.pop("last_photo_name", None)
    request.session["last_focus"] = [0.5, 0.5]
    cj = os.path.join(d, f"{uid}_colors.json")
    colors_list = json.load(open(cj, encoding="utf-8")) if os.path.exists(cj) else []
    colors = len(colors_list) or 24
    w, h, orientation = 40.0, 50.0, "portrait"
    head = open(os.path.join(d, f"{uid}_digipaint.svg"), encoding="utf-8").read(300)
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head)
    if m:
        w, h = round(float(m.group(1)) / 10, 1), round(float(m.group(2)) / 10, 1)
        orientation = "paysage" if w > h else "portrait"
    canvas = compute_price("40x50", colors if colors in (12, 24, 36, 48) else 24)
    request.session["order"] = {
        "uid": uid, "colors": colors, "width_cm": w, "height_cm": h,
        "orientation": orientation, "format_label": "40 x 50 cm",
        "difficulty": "auto", "lang": get_language() or "fr",
        "canvas_price": canvas, "brushes": False, "brushes_amount": 0.0,
        "price": canvas, "colors_list": colors_list,
    }
    return redirect("studio:preview")



def current_photo(request):
    p = request.session.get("last_photo")
    if not p or not os.path.exists(p):
        raise Http404
    resp = FileResponse(open(p, "rb"))
    resp["Cache-Control"] = "no-store, private"                # photo de la session : jamais d'ancienne version en cache
    return resp


def digipaint(request, uid):
    if uid.startswith("gal-gal-"):   # anciens uid de galerie (double prefixe) -> nouvel uid
        return redirect("studio:digipaint", uid=uid[4:], permanent=True)
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    if uid.startswith("lib-") and not os.path.isdir(d):   # anciens modeles du home -> meme modele en galerie
        return redirect("studio:digipaint", uid="gal-" + uid[4:].removeprefix("lib-"), permanent=True)
    if not os.path.exists(os.path.join(d, f"{uid}_digipaint.svg")):
        if uid.startswith(("gal-", "lib-")):
            # Modele de galerie retire (ou ancienne URL indexee) : redirection permanente vers la galerie
            # plutot qu'un 404 (Search Console : pages "Introuvable").
            return redirect("studio:gallery", permanent=True)
        raise Http404
    import json
    order = request.session.get("order")
    palette = order.get("colors_list") if (order and order.get("uid") == uid) else None
    if not palette:
        cj = os.path.join(d, f"{uid}_colors.json")
        if os.path.exists(cj):
            palette = json.load(open(cj, encoding="utf-8"))
        else:
            oj = os.path.join(d, "order.json")
            try:
                palette = json.load(open(oj, encoding="utf-8")).get("colors", []) if os.path.exists(oj) else []
                if not palette:   # commande payee : colors.json nettoye -> couleurs de <uid>_order.json
                    uoj = os.path.join(d, f"{uid}_order.json")
                    if os.path.exists(uoj):
                        palette = [{"number": c.get("number"), "hex": c.get("hex"), "rgb": c.get("rgb")}
                                   for c in json.load(open(uoj, encoding="utf-8")).get("color_specifications", [])]
            except Exception:
                palette = []
    # Toile de la GALERIE : payante -> exiger l'achat ; gratuite -> l'ajouter a Mes Toiles
    lib = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
    buy = None
    owned = DigitalCanvas.objects.filter(uid=uid).exists()
    room = _collab_room(request.GET.get("room", ""))
    room = room if room and room.uid == uid else None
    if lib and not room:
        verified = request.session.get("verified_email")
        owns = (bool(verified) and DigitalCanvas.objects.filter(email=verified, uid=uid).exists()) \
            or uid in request.session.get("dl_uids", [])     # achete sur cet appareil (e-mail saisi dans Stripe)
        if lib.hidden and not owns and not request.user.is_staff:
            return redirect("studio:gallery")          # retire temporairement : seuls ses joueurs y ont acces
        if float(lib.price or 0) > 0 and not owns:
            # modele payant : 5 min d'essai (comme les toiles des commandes), puis achat pour continuer
            buy = {"url": reverse("studio:gallery_buy", args=[uid]), "price": float(lib.price)}
            owned = False                              # essai minute, y compris pour l'equipe (test du parcours)
        elif verified and not owns:
            _grant_gallery(request, lib, verified)   # gratuite -> arrive dans Mes Toiles
    if lib and not room:                               # « Mes toiles » : la toile jouee y apparait (la plus recente en tete)
        played = [u for u in request.session.get("played", []) if u != uid]
        request.session["played"] = ([uid] + played)[:60]
    if room:
        owned = True                                   # invite a peindre a plusieurs : pas de minuteur d'essai
    # Essai gratuit : "Retour a la commande" ramene a l'apercu de la commande en cours.
    # Sortie du jeu (croix, fin d'essai) : retour a l'apercu de la commande en cours, sans rien perdre.
    if request.session.get("digital_trial_uid") == uid and not owned:
        back_url, back_label = reverse("studio:paint_portal"), _("Mes toiles")
    elif order and order.get("uid") == uid:
        back_url, back_label = reverse("studio:preview"), _("Retour à la commande")
    elif uid.startswith(("gal-", "lib-")):
        back_url, back_label = reverse("studio:gallery"), _("Retour aux modèles")
    else:
        back_url, back_label = reverse("studio:paint_portal"), _("Mes toiles")
    return render(request, "studio/digipaint.html",
                  {"uid": uid, "palette": palette, "owned": owned, "back_url": back_url, "back_label": back_label,
                   "buy": buy, "kit_url": reverse("studio:digipaint_buy", args=[uid]),
                   "room": room.token if room else "", "can_collab": bool(room) or _can_collab(request, uid),
                   "share_url": settings.SITE_URL + (reverse("studio:digipaint", args=[uid]) if uid.startswith("gal-") else "/"),
                   "can_dl": can_download(request, uid), "dl_formats": [(i, l) for i, l, _ in download_formats(uid)],
                   "dl_buy": buy["url"] if buy else "",
                   "kit_from": __import__("studio.landings", fromlist=["x"]).facts((get_language() or "fr")[:2])["price_eur"]})


_FILL_RX = None


def _collab_room(token):
    from .models import CollabRoom
    return CollabRoom.objects.filter(token=token).first() if token and len(token) <= 24 else None


def _clean_zones(raw, limit=400):
    """{indice: couleur} venant du navigateur -> valeurs sures (indices entiers, couleurs #hex / rgb())."""
    import re
    global _FILL_RX
    _FILL_RX = _FILL_RX or re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([\d\s.,%]{5,40}\))$")
    out = {}
    for k, v in list((raw or {}).items())[:limit]:
        if str(k).isdigit() and int(k) < 20000 and isinstance(v, str) and _FILL_RX.match(v.strip()):
            out[str(int(k))] = v.strip()
    return out


def _can_collab(request, uid):
    """Inviter des amis : toile gratuite / possedee (pas pendant l'essai d'un modele payant non achete)."""
    lib = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
    if lib and float(lib.price or 0) > 0:
        v = request.session.get("verified_email")
        return bool(v) and DigitalCanvas.objects.filter(email=v, uid=uid).exists()
    return DigitalCanvas.objects.filter(uid=uid).exists()


_PEER_COLORS = ["#e8455f", "#2f6bf2", "#1f9d55", "#f08a3c", "#7a3ff2", "#0ea5b7", "#d63384", "#8a6d1f"]


def _collab_players(token, pid, score=None, name=None, color=None, ttl=45):
    """Joueurs presents sur une toile partagee (cache partage HTTP + WebSocket) : {id: {t, s, n, c}}."""
    import time as _time
    import zlib
    from django.core.cache import cache
    key = "collab-peers:" + token
    peers = cache.get(key) or {}
    now = _time.time()
    if pid:
        p = peers.get(pid) or {"s": 0, "n": "", "c": color or _PEER_COLORS[zlib.crc32(pid.encode()) % len(_PEER_COLORS)]}
        p["t"] = now
        try:
            if score is not None and str(score) != "":
                p["s"] = max(0, min(10 ** 9, int(score)))
        except (TypeError, ValueError):
            pass
        if name:
            p["n"] = str(name)[:16]
        if color:
            p["c"] = color
        peers[pid] = p
    peers = {k: v for k, v in peers.items() if isinstance(v, dict) and now - v.get("t", 0) < ttl}
    cache.set(key, peers, 300)
    return peers


def _players_list(peers):
    return [{"id": k, "s": v.get("s", 0), "n": v.get("n", ""), "c": v.get("c", "#2f6bf2")} for k, v in peers.items()]


def collab_create(request, uid):
    """POST : cree le lien d'invitation, l'etat actuel du createur devient l'etat de depart de la toile partagee."""
    import json
    import secrets
    from .models import CollabRoom
    if request.method != "POST":
        return JsonResponse({"error": "method"}, status=405)
    if not os.path.exists(os.path.join(settings.MEDIA_ROOT, "orders", uid, f"{uid}_digipaint.svg")):
        raise Http404
    if not _can_collab(request, uid):
        return JsonResponse({"error": "locked"}, status=403)
    if security.rate_limited("collab-new:" + security.client_ip(request), 20, 3600):
        return JsonResponse({"error": "rate"}, status=429)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    room = CollabRoom.objects.create(token=secrets.token_urlsafe(12)[:16], uid=uid,
                                     painted=_clean_zones(body.get("painted"), limit=20000), version=1)
    request.session.setdefault("collab_rooms", [])
    request.session["collab_rooms"] = (request.session["collab_rooms"] + [room.token])[-20:]
    return JsonResponse({"token": room.token, "url": "%s%s?room=%s" % (settings.SITE_URL, reverse("studio:digipaint", args=[uid]), room.token)})


def collab_sync(request, token):
    """GET : etat partage (si change depuis ?v=) + nombre de peintres en ligne. POST : zones peintes a ajouter."""
    import json
    import time as _time
    from django.core.cache import cache
    from django.db import transaction
    from .models import CollabRoom
    room = _collab_room(token)
    if not room:
        raise Http404
    cid = (request.GET.get("cid") or "")[:24]
    peers = _collab_players(token, cid, request.GET.get("s"), request.GET.get("n"))
    if request.method == "POST":
        if security.rate_limited("collab-post:" + token + ":" + security.client_ip(request), 240, 60):
            return JsonResponse({"error": "rate"}, status=429)
        try:
            zones = _clean_zones(json.loads(request.body or b"{}").get("zones"))
        except ValueError:
            zones = {}
        if zones:
            with transaction.atomic():
                r = CollabRoom.objects.select_for_update().get(pk=room.pk)
                new = {k: v for k, v in zones.items() if k not in r.painted}
                if new:
                    r.painted.update(new)
                    r.version += 1
                    r.save(update_fields=["painted", "version", "updated_at"])
                room = r
        return JsonResponse({"v": room.version, "peers": len(peers), "players": _players_list(peers)})
    try:
        since = int(request.GET.get("v") or 0)
    except ValueError:
        since = 0
    data = {"v": room.version, "peers": len(peers), "players": _players_list(peers)}
    if since != room.version:
        data["painted"] = room.painted
    return JsonResponse(data)


def preview(request):
    order = request.session.get("order")
    if not order:
        return redirect("studio:upload")
    p = Pricing.get()
    from .models import KitFormat
    colors = order.get("colors", 24)
    land = order.get("orientation") == "paysage"

    def _oriented(kf):   # 40x50 -> 50x40 en paysage (le carre reste carre)
        a, b = sorted((kf.width_cm, kf.height_cm))
        return (b, a) if land else (a, b)
    formats = []
    for kf in KitFormat.objects.filter(available=True):
        fw, fh = _oriented(kf)
        formats.append({"id": kf.id, "label": "%d x %d cm" % (fw, fh), "w": fw, "h": fh,
                        "ratio": round(fw / fh, 3), "price": round(float(kf.price) + p.color_price(colors), 2),
                        "frame": round(float(kf.frame_price), 2) if kf.frame_available else None})
    gw, gh = order.get("gen_w") or order.get("width_cm"), order.get("gen_h") or order.get("height_cm")
    gen_ratio = round(float(gw) / float(gh), 3) if gw and gh else None
    current_format = next((fm["id"] for fm in formats
                           if float(fm["w"]) == float(order.get("width_cm") or 0)
                           and float(fm["h"]) == float(order.get("height_cm") or 0)),
                          (formats[0]["id"] if formats else None))
    # aligne le prix affiche sur le format courant
    if formats:
        cf = next((fm for fm in formats if fm["id"] == current_format), formats[0])
        if order.get("framed") and cf["frame"] is None:
            order["framed"] = False                     # cadre plus propose pour ce format
        order["canvas_price"] = round(cf["price"] + (cf["frame"] if order.get("framed") else 0), 2)
        order["width_cm"], order["height_cm"], order["format_label"] = cf["w"], cf["h"], cf["label"]
        order["price"] = round(cf["price"] + order.get("brushes_amount", 0.0), 2)
        request.session["order"] = order
    # Difficulte reelle de CETTE toile (zones + couleurs), comme les etoiles de la page Jouer
    from .models import difficulty_level
    if not order.get("zones"):
        order["zones"] = _count_zones(order.get("uid", ""))
        request.session["order"] = order
    level = difficulty_level(order.get("zones"), colors)
    from . import currency as fx       # prix dans la devise du visiteur (memes valeurs que chez Stripe)
    cur = fx.for_request(request)
    for fm in formats:
        fm["lp"] = fx.local(fm["price"], cur)
        fm["lf"] = round(fx.local(fm["price"] + fm["frame"], cur) - fm["lp"], 2) if fm["frame"] is not None else None
    sym, before = fx.CURRENCIES.get(cur, ("€", False))
    money_js = {"sym": sym, "before": before, "dec": "." if before or (get_language() or "fr")[:2] == "en" else ","}
    return render(request, "studio/preview.html",
                  {"order": order, "brushes_price": p.brushes_price, "formats": formats, "level": level,
                   "gen_ratio": gen_ratio, "gen_w": gw, "gen_h": gh,
                   "current_format": current_format, "framed": bool(order.get("framed")),
                   "base_local": fx.order_amounts(order, cur)["canvas"], "bp_local": fx.local(p.brushes_price, cur),
                   "money_js": money_js,
                   "frame_any": any(fm["frame"] is not None for fm in formats),
                   "brushes_available": p.av_brushes})


def preview_reformat(request):
    """Format aux proportions differentes (ex. 40x40) choisi dans l'apercu : on recalcule la toile a ces
    dimensions (meme photo, meme cadrage, memes reglages ; lissage/couleurs en cache -> rapide)."""
    order = request.session.get("order")
    if request.method != "POST" or not order:
        return redirect("studio:preview")
    from .models import KitFormat
    kf = KitFormat.objects.filter(id=request.POST.get("kit_format"), available=True).first()
    src = request.session.get("last_photo")
    if not src or not os.path.exists(src):
        from .pipeline import source_file
        src = source_file(os.path.join(settings.MEDIA_ROOT, "orders", order.get("uid", "")), order.get("uid", ""))
    silent = request.headers.get("X-Requested-With") == "fetch"   # recalcul en arriere-plan depuis l'apercu
    if not kf or not src or not os.path.exists(src):
        return JsonResponse({"error": "source"}, status=400) if silent else redirect("studio:preview")
    a, b = sorted((kf.width_cm, kf.height_cm))
    orientation = order.get("orientation", "portrait")
    w, h = (b, a) if orientation == "paysage" else (a, b)
    fx, fy = order.get("focus") or request.session.get("last_focus") or [0.5, 0.5]
    uid = f"{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:4].upper()}"
    jobs.start(uid)
    fmt_key = "40x50" if "40x50" in FORMATS else next(iter(FORMATS))
    try:
        genqueue.submit(_run_generation, uid, src, int(order.get("colors", 24)), w, h, fmt_key, orientation,
                        get_language() or "fr", (fx, fy),
                        source_name=request.session.get("last_photo_name") or os.path.basename(src),
                        level=order.get("difficulty") if order.get("difficulty") in ("easy", "medium", "expert") else "auto")
    except genqueue.QueueFull:
        jobs.pop(uid)
        return JsonResponse({"error": "busy"}, status=503) if silent else redirect("studio:preview")
    request.session.pop("digital_trial", None)   # on reste dans le parcours commande -> retour a l'apercu
    if silent:
        return JsonResponse({"uid": uid})
    return redirect("%s?uid=%s" % (reverse("studio:processing"), uid))


def set_options(request):
    """Depuis l'apercu : option pinceaux, puis vers la livraison."""
    order = request.session.get("order")
    if not order:
        return redirect("studio:upload")
    if request.method == "POST":
        p = Pricing.get()
        from .models import KitFormat
        fmt_id = request.POST.get("kit_format")
        kf = KitFormat.objects.filter(id=fmt_id, available=True).first() if fmt_id else None
        if not kf:      # format courant (pas de changement de format dans le formulaire)
            a, b = sorted((int(order.get("width_cm") or 40), int(order.get("height_cm") or 50)))
            kf = KitFormat.objects.filter(width_cm=a, height_cm=b, available=True).first()
        if kf:
            a, b = sorted((kf.width_cm, kf.height_cm))
            fw, fh = (b, a) if order.get("orientation") == "paysage" else (a, b)
            order["width_cm"], order["height_cm"] = fw, fh
            order["format_label"] = "%d x %d cm" % (fw, fh)
            framed = request.POST.get("framed") == "1" and kf.frame_available
            order["framed"] = framed
            order["canvas_price"] = round(float(kf.price) + p.color_price(order.get("colors", 24))
                                          + (float(kf.frame_price) if framed else 0), 2)
        brushes = bool(request.POST.get("brushes")) and p.av_brushes
        bp = p.brushes_price if brushes else 0.0
        order["brushes"] = brushes
        order["brushes_amount"] = round(bp, 2)
        order["price"] = round(order["canvas_price"] + bp, 2)
        request.session["order"] = order
    return redirect("studio:delivery")


def order_image(request, uid, kind):
    if kind not in ("preview", "template", "digipaint", "poster", "source"):
        raise Http404
    if kind == "source":
        # Photo d'origine du client : donnee personnelle -> staff, proprietaire (session) ou modele de galerie.
        own = uid == (request.session.get("order") or {}).get("uid") or uid in request.session.get("my_uids", [])
        if not (request.user.is_staff or own or uid.startswith(("gal-", "lib-", "mkt-"))):
            raise Http404
        from .pipeline import source_file
        path = source_file(os.path.join(settings.MEDIA_ROOT, "orders", uid), uid)
    else:
        base = os.path.join(settings.MEDIA_ROOT, "orders", uid, f"{uid}_{kind}")
        path = base + ".svg" if os.path.exists(base + ".svg") else base + ".png"
    if not os.path.exists(path):
        raise Http404
    ctype = "image/svg+xml" if path.endswith(".svg") else "image/png"
    resp = FileResponse(open(path, "rb"), content_type=ctype)
    resp["Cache-Control"] = "no-store, max-age=0"
    resp["X-Robots-Tag"] = "noindex, nofollow, noimageindex"
    return resp


def production_page(request, uid, sig):
    """Page fournisseur (lien court dans la remarque de commande ERP) : infos + tous les fichiers de fabrication.
    Jamais la photo d'origine du client ni de prix."""
    if "/" in uid or uid.startswith(".") or not security.check_production(uid, sig):
        raise Http404
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    if not os.path.isdir(d):
        raise Http404
    fulfillment.ensure_supplier_files(uid)
    rows = [("Numbered canvas (print file) / 数字画布（印刷文件）", "%s_template.tiff" % uid),
            ("Poster: print it and put it in the parcel / 海报：请打印并放入包裹", "%s_poster.png" % uid),
            ("Colour table / 色卡", "%s_supplier_colors.jpg" % uid),
            ("Order sheet / 订单信息", "%s_supplier.txt" % uid)]
    files = [(label, n, security.file_url(uid, n, absolute=False)) for label, n in rows if os.path.isfile(os.path.join(d, n))]
    txt = ""
    try:
        with open(os.path.join(d, "%s_supplier.txt" % uid), encoding="utf-8") as fh:
            txt = fh.read()
    except OSError:
        pass
    imgs = [security.file_url(uid, n, absolute=False) for n in ("%s_supplier_colors.jpg" % uid, "%s_poster.png" % uid)
            if os.path.isfile(os.path.join(d, n))]
    resp = render(request, "studio/production.html", {"uid": uid, "files": files, "txt": txt, "imgs": imgs,
                                                      "zip": security.file_url(uid, "production.zip", absolute=False)})
    resp["X-Robots-Tag"] = "noindex, nofollow"
    resp["Cache-Control"] = "private, no-store"
    return resp


def order_file(request, uid, name):
    """Fichier de media/orders/<uid>/ via lien signe (fournisseurs) ou pour le staff.
    Le dossier n'est plus servi publiquement par nginx (donnees clients)."""
    if "/" in name or "\\" in name or name.startswith(".") or "/" in uid or uid.startswith("."):
        raise Http404
    if not (request.user.is_staff or security.check_file_token(uid, name, request.GET.get("t", ""))):
        raise Http404
    if name.endswith("_supplier_card.jpg"):
        # Image de ligne dans l'ERP : affichee en miniature, mais « 查看原图 » (ouverture dans un onglet)
        # mene directement a la page de tous les fichiers -> lien cliquable depuis le tableau des commandes.
        dest = request.headers.get("Sec-Fetch-Dest", "")
        if dest == "document" or (not dest and "text/html" in request.headers.get("Accept", "")):
            return redirect(security.production_url(uid, absolute=False))
    if name == "production.zip":
        # Tous les fichiers de production d'une commande en un seul lien (fournisseur / ERP) - sans prix
        import io, zipfile
        # Ce dont le fournisseur a besoin, et seulement ca : poster, toile .tiff, JSON (couleurs -> references nuancier)
        import json as _json
        d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
        names = ["%s_poster.png" % uid, "%s_template.tiff" % uid, "%s_supplier.json" % uid]
        fulfillment.ensure_supplier_files(uid)              # + fiche texte EN/中文 et tableau des couleurs JPG
        extra = ["%s_supplier.txt" % uid, "%s_supplier_colors.jpg" % uid]
        found = [n for n in names + extra if os.path.isfile(os.path.join(d, n))]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for n in found:
                z.write(os.path.join(d, n), n)
            oj = os.path.join(d, "%s_order.json" % uid)
            if names[2] not in found and os.path.isfile(oj):
                # repli : JSON fournisseur derive de la fiche commande, SANS prix ni infos internes
                full = _json.load(open(oj, encoding="utf-8"))
                oi = full.get("order_information") or {}
                sj = {"consignee_information": full.get("consignee_information") or {},
                      "order_information": {k: oi.get(k) for k in ("uid", "created_at", "format", "orientation", "width_cm",
                                                                   "height_cm", "colors_count", "brushes")},
                      "color_specifications": fulfillment.add_areas(
                          uid, fulfillment.supplier_colors(full.get("color_specifications") or []),
                          oi.get("width_cm"), oi.get("height_cm"), full.get("color_specifications") or [])}
                sj["order_information"]["files"] = {"template_tiff": names[1], "poster_png": names[0]}
                z.writestr(names[2], _json.dumps(sj, ensure_ascii=False, indent=2))
                found.append(names[2])
        if not found:
            raise Http404
        resp = HttpResponse(buf.getvalue(), content_type="application/zip")
        resp["Content-Disposition"] = 'attachment; filename="PaintIt-%s.zip"' % uid
        resp["X-Robots-Tag"] = "noindex, nofollow"
        return resp
    path = os.path.join(settings.MEDIA_ROOT, "orders", uid, name)
    if not os.path.isfile(path):
        raise Http404
    resp = FileResponse(open(path, "rb"), as_attachment=not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".pdf")))
    resp["X-Robots-Tag"] = "noindex, nofollow"
    resp["Cache-Control"] = "private, no-store"
    return resp


def my_models(request):
    return redirect("studio:paint_portal")


def paint_portal(request):
    verified = request.session.get("verified_email", "")
    own = list(DigitalCanvas.objects.filter(email=verified)) if verified else []
    free_used = (len(own) >= 1) if verified else bool(request.session.get("free_used"))
    # toiles de la galerie jouees sur cet appareil : en tete, dans l'ordre de la derniere partie
    played = request.session.get("played", [])
    libs = {m.uid: m for m in DigitalCanvas.objects.filter(email="__library__", uid__in=played, hidden=False)}
    own_by_uid = {c.uid: c for c in own}
    canvases, seen = [], set()
    for uid in played:
        c = own_by_uid.get(uid)
        if not c and uid in libs:
            m = libs[uid]
            if verified and not float(m.price or 0) > 0:
                _grant_gallery(request, m, verified)          # gratuite : rattachee au compte (tous ses appareils)
                c = DigitalCanvas.objects.filter(email=verified, uid=uid).first()
            if not c:
                c = m
                c.trial = float(m.price or 0) > 0             # modele payant en essai
        if c and uid not in seen:
            canvases.append(c); seen.add(uid)
    canvases += [c for c in own if c.uid not in seen]
    return render(request, "studio/portal.html", {
        "verified_email": verified, "canvases": canvases,
        "free_used": free_used, "credits": request.session.get("paid_credits", 0),
    })


def _count_zones(uid):
    f = os.path.join(settings.MEDIA_ROOT, "orders", uid, f"{uid}_digipaint.svg")
    try:
        with open(f, encoding="utf-8") as fh:
            return fh.read().count('class="z"')
    except OSError:
        return 0


def legacy_play(request, uid=None, rest=None):
    """Anciennes URLs /gallery/... et /paint/<uid>/ -> /play/... (301, conserve le prefixe de langue)."""
    path = request.path.replace("/gallery/", "/play/", 1) if "/gallery/" in request.path \
        else request.path.replace("/paint/", "/play/", 1)
    q = request.META.get("QUERY_STRING")
    return HttpResponsePermanentRedirect(path + ("?" + q if q else ""))


def gallery(request):
    """Onglet Gallery : toutes les toiles PaintIt en vrac, filtrables par recherche."""
    models = list(DigitalCanvas.objects.filter(email="__library__", uid__startswith="gal-", hidden=False).order_by("category", "title"))
    for m in models:   # nb de zones calcule une fois depuis le SVG du jeu, puis memorise
        if not m.zones:
            m.zones = _count_zones(m.uid)
            if m.zones:
                m.save(update_fields=["zones"])
    from . import seo
    from django.utils.translation import gettext as _t
    models.sort(key=lambda m: bool(m.price and m.price > 0))   # gratuites d'abord : envie de tester (tri stable)
    from collections import Counter
    from . import categories
    lang = get_language() or "fr"
    art = [m for m in models if categories.label(m.category, "en") == "Famous paintings"]
    if art:                                  # tableaux celebres : titre de l'oeuvre + peintre (survol)
        from .models import StockImage
        painter = {}
        for used, author in StockImage.objects.filter(used_in__contains="catalogue gal-").values_list("used_in", "author"):
            for part in used.split(", "):
                if part.startswith("catalogue "):
                    painter[part[10:]] = " ".join((author or "").split())[:60]
        for m in art:
            m.art_caption = " · ".join(x for x in (m.title, painter.get(m.uid, "")) if x)
    for m in models:
        m.cat_label = categories.label(m.category, lang) if m.category else ""
    cats = Counter(m.category for m in models if m.category)
    n_free = sum(1 for m in models if not (m.price and m.price > 0))
    return render(request, "studio/gallery.html", {
        "models": models, "n_free": n_free, "n_paid": len(models) - n_free,
        "rng": {"cmin": min((m.colors or 24) for m in models) if models else 0,
                "cmax": max((m.colors or 24) for m in models) if models else 0,
                "zmin": min((m.zones or 0) for m in models) if models else 0,
                "zmax": max((m.zones or 0) for m in models) if models else 0},
        "cats": sorted(((c, categories.label(c, lang), n) for c, n in cats.items()), key=lambda x: x[1].lower()),
        "ld_list": seo.dump(seo.item_list(models, lambda m: reverse("studio:digipaint", args=[m.uid]))),
        "ld_crumb": seo.dump(seo.breadcrumb((_t("Accueil"), "/"), (_t("Testez notre jeu"), request.path)))})


def paint_send_code(request):
    if request.method != "POST":
        return redirect("studio:paint_portal")
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError
    email = (request.POST.get("email") or "").strip()
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({"error": "email"}, status=400)
    ip = security.client_ip(request)
    if security.rate_limited("code-mail:" + email.lower(), 3, 600) or security.rate_limited("code-ip:" + ip, 10, 3600):
        return JsonResponse({"error": "rate"}, status=429)
    code = security.random_code()
    EmailCode.objects.filter(email=email).delete()     # un seul code valide a la fois
    EmailCode.objects.create(email=email, code=code)
    emails.send_code(email, code, get_language() or "fr")
    return JsonResponse({"ok": True})


def paint_verify_code(request):
    if request.method != "POST":
        return redirect("studio:paint_portal")
    from datetime import timedelta
    from django.utils import timezone
    email = (request.POST.get("email") or "").strip()
    code = (request.POST.get("code") or "").strip()
    if security.rate_limited("code-try:" + email.lower(), 5, 900) \
            or security.rate_limited("code-try-ip:" + security.client_ip(request), 30, 900):
        EmailCode.objects.filter(email=email).delete()   # trop d'essais : le code est grille
        return JsonResponse({"error": "rate"}, status=429)
    ok = EmailCode.objects.filter(email=email, code=code,
                                  created_at__gte=timezone.now() - timedelta(minutes=15)).exists()
    if not ok:
        return JsonResponse({"error": "code"}, status=400)
    EmailCode.objects.filter(email=email).delete()       # usage unique
    request.session["verified_email"] = email
    _bp = request.session.pop("buy_pending", None)          # achat en attente : on le reprend la ou il en etait
    nxt = _bp if (_bp or "").startswith("/") else (("/play/buy/%s/" % _bp) if _bp else "/paint/")
    return JsonResponse({"ok": True, "redirect": nxt})


def paint_logout(request):
    request.session.pop("verified_email", None)
    return redirect("studio:paint_portal")


def paint_link_email(request):
    """Lie la galerie a un e-mail (verification reelle prevue en production).
    Rattache aussi les toiles creees pendant la session a cet e-mail."""
    if request.method != "POST":
        return redirect("studio:paint_portal")
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError
    email = (request.POST.get("email") or "").strip()
    try:
        validate_email(email)
    except ValidationError:
        return redirect("studio:paint_portal")
    request.session["verified_email"] = email
    for uid in request.session.get("my_uids", []):
        recs = DigitalCanvas.objects.filter(uid=uid)
        rec = recs.filter(email="").first() or recs.first()
        if rec and not DigitalCanvas.objects.filter(email=email, uid=uid).exists():
            rec.email = email
            rec.save(update_fields=["email"])
    return redirect("studio:paint_portal")


def paint_new(request):
    verified = request.session.get("verified_email", "")
    count = (DigitalCanvas.objects.filter(email=verified).count() if verified
             else (1 if request.session.get("free_used") else 0))
    credits = request.session.get("paid_credits", 0)
    if count < 1:
        request.session["digital_mode"] = True
        return redirect("studio:upload")
    if credits > 0:
        request.session["paid_credits"] = credits - 1
        request.session["digital_mode"] = True
        return redirect("studio:upload")
    # Toiles suivantes (« Ajouter · 0,99 € ») : apres generation on va DIRECT au jeu (pas l'apercu de commande) ;
    # la toile n'entre dans Mes toiles qu'apres paiement (mur 0,99 EUR dans le jeu).
    return redirect(reverse("studio:upload") + "?trial=1")


def paint_pay(request):
    # Ancien credit "toile supplementaire" offert sans paiement : supprime (faille).
    return redirect("studio:upload")


def paint_unlock(request, uid):
    """Essai gratuit : on entre directement dans le jeu (2 min), le paiement se fait ensuite
    via le mur 'pour continuer' (paint_confirm)."""
    return redirect("studio:digipaint", uid=uid)


def _credit_digital(request, uid, sess=None):
    """Enregistre/deverrouille la toile numerique : rangee sous l'e-mail de l'acheteur, utilisable tout de suite
    sur cet appareil (session)."""
    email = _buyer_email(request, sess)
    if _record_digital(uid, email, request.session.get("order") or {}) and sess and email:
        _digital_receipt(email, uid, "", sess)
    request.session["free_used"] = True
    uids = request.session.get("my_uids", [])
    if uid not in uids: uids.append(uid); request.session["my_uids"] = uids[-60:]


def _record_digital(uid, verified, o):
    """Toile numerique payee -> fiche DigitalCanvas (Mes toiles). Aussi appele par le webhook Stripe."""
    import os, json as _json
    o = o if o.get("uid") == uid else {}
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    colors = o.get("colors", 24); ori = o.get("orientation", "portrait"); w = o.get("width_cm", 40); h = o.get("height_cm", 50)
    cj = os.path.join(d, f"{uid}_colors.json")
    if os.path.exists(cj):
        try: colors = len(_json.load(open(cj, encoding="utf-8"))) or colors
        except Exception: pass
    try:
        return DigitalCanvas.objects.update_or_create(
            email=verified, uid=uid,
            defaults=dict(colors=colors, orientation=ori, width_cm=w, height_cm=h, source="digital"))[1]
    except Exception:
        logger.exception("Toile numerique %s", uid)
    return False


def _digital_receipt(email, uid, title, sess, lang=None):
    """E-mail de confirmation d'achat (une seule fois : appele par celui qui cree la toile, webhook ou retour)."""
    try:
        amount = (sess.get("amount_total") or 0) / 100.0 if sess else 0.0
        emails.send_digital_receipt(email, uid, title, amount,
                                    lang or ((sess or {}).get("locale") or "") or get_language() or "fr",
                                    cur=((sess or {}).get("currency") or "eur"))
    except Exception:
        logger.exception("Confirmation achat numerique %s", uid)


def _buyer_email(request, sess=None):
    """E-mail de l'acheteur : e-mail verifie de la session, sinon celui saisi dans Stripe (sous sa responsabilite).
    Sert a ranger la toile achetee et a envoyer le recu ; il ne connecte PAS a « Mes toiles » (code requis)."""
    v = request.session.get("verified_email") if request is not None else ""
    if v:
        return v
    s = sess or {}
    return ((s.get("customer_details") or {}).get("email") or s.get("customer_email") or "").strip()


def paint_confirm(request, uid):
    """Boutons d'achat numerique (jeu : « Sauvegarder », apercu / jeu : « Télécharger ma toile ») -> URL suivante :
    identification par e-mail si besoin, puis paiement (paint_buy)."""
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    dl = request.POST.get("dl") == "1" or request.GET.get("dl") == "1"
    return JsonResponse({"ok": True, "redirect": reverse("studio:paint_buy", args=[uid]) + ("?dl=1" if dl else "")})


def paint_buy(request, uid):
    """Achat de la toile numerique (0,99 €, ou equivalent en devise) : Stripe (qui demande l'e-mail) -> retour.
    `dl=1` : achat pour telecharger -> page de telechargement apres paiement."""
    if "/" in uid or uid.startswith(".") or not os.path.isdir(os.path.join(settings.MEDIA_ROOT, "orders", uid)):
        raise Http404
    dl = request.GET.get("dl") == "1"
    page = reverse("studio:paint_download_page", args=[uid])
    verified = request.session.get("verified_email") or None       # sinon Stripe demande l'e-mail
    if dl and can_download(request, uid, staff=False):     # deja payee par ce client : pas de second paiement
        return redirect(page)
    if payments.stripe_live():
        try:
            return redirect(payments.create_digital_session(uid, request, email=verified, download=dl))
        except Exception:
            logger.exception("Stripe digital session %s", uid)
            return render(request, "studio/download.html", {"uid": uid, "pay_error": True, "dl_formats": []}, status=502)
    if not payments.demo_allowed():
        return render(request, "studio/download.html", {"uid": uid, "pay_error": True, "dl_formats": []}, status=503)
    # Demo (dev uniquement) : pas de paiement reel -> credit immediat.
    _credit_digital(request, uid)
    _grant_download(request, uid)
    return redirect((page if dl else reverse("studio:digipaint", args=[uid])) + "?bought=1")


def paint_unlock_success(request):
    """Retour Stripe apres paiement de la toile numerique : credite puis ouvre le jeu."""
    uid = request.GET.get("uid", "")
    sid = request.GET.get("sid", "")
    sess = payments.paid_session(sid, uid=uid, kind="digital") if uid else None
    if sess:
        _credit_digital(request, uid, sess)
        _grant_download(request, uid)
        if request.GET.get("dl"):        # achat depuis « Télécharger ma toile » : page de telechargement, pas le jeu
            return redirect(reverse("studio:paint_download_page", args=[uid]) + "?bought=1")
        return redirect(reverse("studio:digipaint", args=[uid]) + "?bought=1")
    return redirect("studio:digipaint", uid=uid)


def _grant_download(request, uid):
    dl = [u for u in request.session.get("dl_uids", []) if u != uid]
    request.session["dl_uids"] = (dl + [uid])[-60:]


def can_download(request, uid, staff=True):
    """Telecharger les fichiers imprimables : toile ACHETEE (jeu 0,99 €, modele payant, ou kit commande).
    Les toiles seulement jouees / offertes passent par l'achat. `staff=False` : l'equipe passe aussi par le
    paiement (test du parcours depuis l'apercu)."""
    if (staff and request.user.is_staff) or uid in request.session.get("dl_uids", []):
        return True
    v = (request.session.get("verified_email") or "").strip()
    if not v:
        o = request.session.get("order") or {}
        return o.get("uid") == uid and Order.objects.filter(uid=uid, status__in=Order.PAID_STATUSES).exists()
    from .models import LedgerEntry
    return (LedgerEntry.objects.filter(uid=uid, kind="sale", email__iexact=v).exists()
            or Order.objects.filter(uid=uid, customer_email__iexact=v, status__in=Order.PAID_STATUSES).exists())


# Formats papier proposes au telechargement (portrait, mm) ; les formats du kit (toile) s'y ajoutent.
PAPER_FORMATS = [("cp", "Carte postale 10 x 15 cm", (100, 150)), ("a6", "A6 · 10,5 x 14,8 cm", (105, 148)),
                 ("a5", "A5 · 14,8 x 21 cm", (148, 210)), ("a4", "A4 · 21 x 29,7 cm", (210, 297)),
                 ("letter", "US Letter · 21,6 x 27,9 cm", (216, 279)), ("a3", "A3 · 29,7 x 42 cm", (297, 420)),
                 ("a2", "A2 · 42 x 59,4 cm", (420, 594)), ("a1", "A1 · 59,4 x 84,1 cm", (594, 841))]
DL_MARGINS = (0, 5, 10, 20, 40)      # marge blanche (mm) : 5 mm = imprimante de bureau, 40 mm = bord pour chassis


def download_formats(uid):
    """Formats proposes au telechargement, orientes comme la toile : [(cle, libelle, (L, H) mm)].
    Taille d'origine en tete (choix par defaut), puis formats papier (carte postale -> A1), puis formats toile."""
    from .models import KitFormat
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    try:
        from .printfiles import parse_template
        with open(os.path.join(d, "%s_template.svg" % uid), encoding="utf-8") as fh:
            w, h = parse_template(fh.read())[:2]
    except (OSError, AttributeError, ValueError):
        return []
    land = w > h

    def orient(mm):
        a, b = sorted(mm)
        return (b, a) if land else (a, b)
    canvas = []
    for kf in KitFormat.objects.filter(available=True):
        fw, fh = orient((kf.width_cm * 10, kf.height_cm * 10))
        canvas.append(("t%d" % kf.id, "%s · %d x %d cm" % (_("Toile"), fw // 10, fh // 10), (fw, fh)))
    nat = (int(round(w / 10)) * 10, int(round(h / 10)) * 10)
    same = [f for f in canvas if f[2] == nat]
    first = same or [("orig", "%s · %d x %d cm" % (_("Toile"), nat[0] // 10, nat[1] // 10), (w, h))]
    paper = [(k, l.replace("Carte postale", str(_("Carte postale"))), orient(mm)) for k, l, mm in PAPER_FORMATS]
    return first + paper + [f for f in canvas if f not in same]


def paint_download_page(request, uid):
    """Apres achat depuis l'apercu : choix du format puis telechargement (le jeu ne s'ouvre pas)."""
    if "/" in uid or uid.startswith(".") or not os.path.exists(
            os.path.join(settings.MEDIA_ROOT, "orders", uid, "%s_template.svg" % uid)):
        raise Http404
    if not can_download(request, uid):
        return redirect(reverse("studio:digipaint", args=[uid]) + "?dl=1")
    return render(request, "studio/download.html", {"uid": uid, "dl_formats": [(i, l) for i, l, _ in download_formats(uid)]})


def paint_download(request, uid):
    """ZIP genere a la demande (fichiers temporaires) : toile numerotee TIFF au format choisi + poster."""
    import tempfile, zipfile, shutil
    from django.http import FileResponse
    if "/" in uid or uid.startswith("."):
        raise Http404
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    svgp = os.path.join(d, "%s_template.svg" % uid)
    if not os.path.exists(svgp):
        raise Http404
    if not can_download(request, uid):
        return redirect(reverse("studio:digipaint", args=[uid]))
    if security.rate_limited("dl:" + security.client_ip(request), 30, 3600):
        return HttpResponse(_("Trop de téléchargements en peu de temps. Réessayez plus tard."), status=429)
    fmts = download_formats(uid)
    key = request.GET.get("f") or (fmts[0][0] if fmts else "")
    fmt = next((f for f in fmts if f[0] == key), fmts[0] if fmts else ("", "", None))
    try:
        margin = int(request.GET.get("m", 5))
    except ValueError:
        margin = 5
    margin = margin if margin in DL_MARGINS else 5
    from .printfiles import render_tiff
    from . import fulfillment
    tmp = tempfile.mkdtemp(prefix="pit-dl-")
    try:
        size = "%gx%gcm" % (fmt[2][0] / 10, fmt[2][1] / 10) if fmt[2] else ""
        name = "-".join(x for x in ("PaintIt", uid, fmt[0] if fmt[0] in dict((k, 1) for k, _l, _m in PAPER_FORMATS) else "",
                                    size) if x)
        with open(svgp, encoding="utf-8") as fh:
            render_tiff(fh.read(), os.path.join(tmp, name + ".tiff"), dpi=300, page_mm=fmt[2], margin_mm=margin)
        files = [name + ".tiff"]
        if os.path.exists(os.path.join(d, "%s_preview.png" % uid)):
            try:
                cols = fulfillment.load_colors(uid)
                if cols:
                    fulfillment._compose_poster(uid, d, cols, (get_language() or "fr")[:2], simple=True,
                                                out=os.path.join(tmp, "PaintIt-%s-poster.png" % uid))
                    files.append("PaintIt-%s-poster.png" % uid)
            except Exception:
                logger.exception("Poster telechargement %s", uid)
        zp = os.path.join(tmp, name + ".zip")
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(os.path.join(tmp, f), f)
        fh = open(zp, "rb")              # fichier ouvert : le dossier temporaire peut etre efface tout de suite
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    resp = FileResponse(fh, as_attachment=True, filename=name + ".zip", content_type="application/zip")
    resp["Cache-Control"] = "private, no-store"
    resp["X-Robots-Tag"] = "noindex"
    return resp


def paint_delete(request, uid):
    if request.method != "POST":
        return redirect("studio:paint_portal")
    verified = request.session.get("verified_email", "")
    request.session["played"] = [u for u in request.session.get("played", []) if u != uid]
    if verified:
        DigitalCanvas.objects.filter(uid=uid, email=verified).delete()
    # Modele de la galerie : on le retire seulement de « Mes toiles », ses fichiers servent a tout le monde.
    if DigitalCanvas.objects.filter(email="__library__", uid=uid).exists() or uid.startswith(("gal-", "lib-")):
        return JsonResponse({"ok": True})
    # Suppression des fichiers seulement si aucune commande physique n'y est liee.
    if not Order.objects.filter(uid=uid).exists():
        import shutil
        for u in (uid, uid + "-G"):
            d = os.path.join(settings.MEDIA_ROOT, "orders", u)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
    return JsonResponse({"ok": True})


def print_page(request, uid):
    from .models import PrintPricing
    if not os.path.exists(os.path.join(settings.MEDIA_ROOT, "orders", uid, f"{uid}_preview.svg")):
        raise Http404
    pp = PrintPricing.get()
    return render(request, "studio/print.html", {
        "uid": uid, "variant": request.session.get("order_variant", "painted"),
        "dims": pp.dims(), "materials": pp.materials(), "frames": pp.frames(),
        "glass_price": pp.glass_price if pp.av_glass else None,
        "cfg": pp.cfg(),
        "notified": request.session.pop("print_notified", False),
    })


def print_notify(request, uid):
    """Liste d'attente Tableau fini (commandes pas encore ouvertes)."""
    from .models import ContactMessage
    if request.method == "POST" and security.rate_limited("notify:" + security.client_ip(request), 5, 3600):
        return redirect("studio:print_page", uid=uid)
    if request.method == "POST":
        from django.core.validators import validate_email
        from django.core.exceptions import ValidationError
        email = (request.POST.get("email") or "").strip()
        try:
            validate_email(email)
            ContactMessage.objects.create(
                name="Liste d'attente Tableau fini", email=email,
                subject="Tableau fini , interet", message=f"Prevenir a l'ouverture (uid={uid}).")
            request.session["print_notified"] = True
        except ValidationError:
            pass
    return redirect("studio:print_page", uid=uid)


def print_place(request, uid):
    # Commandes Tableau fini pas encore ouvertes (en attente de fournisseur).
    return redirect("studio:print_page", uid=uid)
    from .models import PrintPricing  # noqa: (desactive)
    if request.method != "POST":
        return redirect("studio:print_page", uid=uid)
    pp = PrintPricing.get()
    cfg = pp.cfg()
    dim = request.POST.get("dim", "")
    mat = request.POST.get("material", "toile")
    frame = request.POST.get("frame", "sans")
    is_toile = (mat == "toile")
    if not is_toile:
        frame = "sans"
    glass = is_toile and request.POST.get("glass") == "on" and cfg["glass"] is not None
    if dim not in cfg["dims"]:
        return redirect("studio:print_page", uid=uid)
    total = cfg["dims"][dim] + cfg["materials"].get(mat, 0) + cfg["frames"].get(frame, 0) + (cfg["glass"] if glass else 0)
    total = round(total, 2)
    email = (request.POST.get("email") or "").strip()
    # Artwork = etat peint courant (SVG) envoye par le client.
    artwork = request.POST.get("artwork", "")
    artwork_ref = ""
    if artwork.strip().startswith("<svg") and len(artwork) < 4_000_000:
        d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
        os.makedirs(d, exist_ok=True)
        artwork_ref = f"{uid}_print_artwork.svg"
        try:
            with open(os.path.join(d, artwork_ref), "w", encoding="utf-8") as f:
                f.write(artwork)
        except Exception:
            artwork_ref = ""
    spec = {"uid": uid, "product": "tableau_fini", "dimension": dim, "material": mat,
            "frame": frame, "glass": glass, "total": total, "artwork": artwork_ref or "(rendu design)",
            "customer": {"name": request.POST.get("full_name", ""), "email": email,
                         "phone": (request.POST.get("phone_code", "") + " " + request.POST.get("phone", "")).strip(),
                         "address1": request.POST.get("address1", ""),
                         "address2": request.POST.get("address2", ""),
                         "postal_code": request.POST.get("postal_code", ""),
                         "city": request.POST.get("city", ""),
                         "country": request.POST.get("country", "")}}
    # E-mail au fournisseur IMPRESSION (distinct du kit) ; demo si non configure.
    dest = pp.supplier_email or settings.SUPPORT_EMAIL
    try:
        from django.core.mail import EmailMessage
        import json
        body = "Nouvelle commande Tableau fini (impression)\n\n" + json.dumps(spec, ensure_ascii=False, indent=2)
        EmailMessage("[PaintIt] Commande Tableau fini %s" % uid, body,
                     settings.DEFAULT_FROM_EMAIL, [dest]).send(fail_silently=True)
    except Exception:
        pass
    return render(request, "studio/print_confirm.html", {"uid": uid, "spec": spec, "total": total})


def digipaint_order(request, uid):
    """Depuis le jeu : retour au parcours classique (recadrage/orientation/couleurs)."""
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    from .pipeline import source_file
    src = source_file(d, uid)
    if src and os.path.exists(src):
        request.session["last_photo"] = src
    import re
    colors, orientation = 24, "portrait"
    cj = os.path.join(d, f"{uid}_colors.json")
    if os.path.exists(cj):
        import json
        try:
            colors = len(json.load(open(cj, encoding="utf-8"))) or 24
        except Exception:
            pass
    dp = os.path.join(d, f"{uid}_digipaint.svg")
    if os.path.exists(dp):
        head = open(dp, encoding="utf-8").read(300)
        m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', head)
        if m and float(m.group(1)) > float(m.group(2)):
            orientation = "paysage"
    request.session["order"] = {"orientation": orientation,
                                "colors": colors if colors in (12, 24, 36, 48) else 24}
    request.session["buy_mode"] = True
    return redirect("studio:upload")


def _run_game_generation(gameuid, src, colors, w, h, detail=1.0, source_name=None, max_zones=None, min_zone_mm=None, density=None):
    def cb(pct, label):
        jobs.update(gameuid, pct=int(pct), label=label)
    try:
        result = generate(src, colors, w, h, uid=gameuid, progress=cb, focus=(0.5, 0.5), detail=detail, source_name=source_name, max_zones=max_zones, min_zone_mm=min_zone_mm, density=density)
        cl = result.get("colors_list", [])
        jobs.update(gameuid, pct=100, label="final", done=True, order={"colors_list": cl})
        # Met a jour le nombre de couleurs de la toile (cartes "mes designs")
        try:
            base_uid = gameuid[:-2] if gameuid.endswith("-G") else gameuid
            if cl:
                DigitalCanvas.objects.filter(uid=base_uid).update(colors=len(cl))
        except Exception:
            pass
    except Exception as exc:                       # pragma: no cover
        logger.exception("Echec generation jeu %s (colors=%s, min_zone_mm=%s, density=%s, max_zones=%s)",
                         gameuid, colors, min_zone_mm, density, max_zones)
        jobs.update(gameuid, done=True, error=str(exc))
    finally:
        from django.db import connections
        connections.close_all()


def digipaint_regen(request, uid):
    # Regeneration lourde (CPU) : plus exposee aux visiteurs depuis la suppression des Reglages du jeu.
    if request.method != "POST" or not request.user.is_staff:
        raise Http404
    from .pipeline import source_file
    src = source_file(os.path.join(settings.MEDIA_ROOT, "orders", uid), uid)
    if not src or not os.path.exists(src):
        return JsonResponse({"error": "source"}, status=404)
    order = request.session.get("order") or {}
    # Dimensions : session si meme uid, sinon fiche DigitalCanvas, sinon 40x50
    if order.get("uid") == uid and order.get("width_cm"):
        w_cm, h_cm = order["width_cm"], order["height_cm"]
    else:
        dc = DigitalCanvas.objects.filter(uid=uid).first()
        w_cm = (dc.width_cm if dc else None) or 40
        h_cm = (dc.height_cm if dc else None) or 50
    try:
        colors = int(request.POST.get("colors", "24"))
    except (TypeError, ValueError):
        colors = 24
    colors = max(2, min(99, colors))
    detail = {"facile": 1.5, "moyen": 1.0, "difficile": 0.7, "extreme": 0.5}.get(
        request.POST.get("difficulty", "moyen"), 1.0)
    def _fopt(name, lo, hi):
        try:
            v = float(request.POST.get(name, "") or 0)
        except (TypeError, ValueError):
            return None
        return max(lo, min(hi, v)) if v else None
    max_zones = int(_fopt("max_zones", 2, 9999)) if _fopt("max_zones", 2, 9999) else None
    min_zone_mm = _fopt("min_zone_mm", 0.5, 20)
    density = _fopt("density", 0.02, 5)
    if security.rate_limited("gen:" + security.client_ip(request), 40, 3600):
        return JsonResponse({"ok": False, "error": "rate_limited"}, status=429)
    gameuid = uid + "-G"
    jobs.start(gameuid)
    try:
        genqueue.submit(_run_game_generation, gameuid, src, colors, w_cm, h_cm, detail,
                        source_name=os.path.basename(src), max_zones=max_zones,
                        min_zone_mm=min_zone_mm, density=density)
    except genqueue.QueueFull:
        jobs.pop(gameuid)
        return JsonResponse({"error": "busy"}, status=429)
    from . import analytics
    analytics.log_event(request, "generation", "Jeu DigiPaint")
    request.session["game_uid"] = gameuid
    return JsonResponse({"job": gameuid})


def digipaint_palette(request, uid):
    gameuid = uid + "-G"
    job = jobs.get(gameuid)
    if not job:
        return JsonResponse({"pending": True})
    if not job.get("done"):
        return JsonResponse({"pending": True, "pct": job.get("pct", 0)})
    if job.get("error"):
        return JsonResponse({"error": job["error"]}, status=500)
    return JsonResponse({"palette": (job.get("order") or {}).get("colors_list", [])})


def robots(request):
    site = settings.SITE_URL
    ai = ("GPTBot", "OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Perplexity-User", "ClaudeBot",
          "Claude-SearchBot", "Claude-User", "Google-Extended", "Applebot-Extended", "Bingbot", "CCBot")
    priv = ("Disallow: /preview/\nDisallow: /media/\nDisallow: /files/\n"
            "Disallow: /delivery/\nDisallow: /checkout/\nDisallow: /order/\nDisallow: /admin/\nDisallow: /admin-hub/\nDisallow: /go/\nDisallow: /avis/\nDisallow: /wc-api/\nDisallow: /wp-json/\nDisallow: /a/\n")
    body = (
        "# Assistants IA et moteurs de recherche : bienvenue (contenu public uniquement)\n"
        + "".join("User-agent: %s\n" % a for a in ai) + "Allow: /llm.txt\nAllow: /llm.json\n" + priv + "\n"
        "User-agent: *\n" + priv +
        "Allow: /llm.txt\nAllow: /llm.json\nAllow: /play/\n"
        "\nSitemap: %s/sitemap.xml\n"
        "\n# Infos pour les IA / LLM :\n"
        "# %s/llm.txt\n# %s/llm.json\n" % (site, site, site)
    )
    return HttpResponse(body, content_type="text/plain")


def devtools(request):
    return HttpResponse(status=204)


# ---------------- Delivery / discount ----------------
def delivery(request):
    order = request.session.get("order")
    if not order:
        return redirect("studio:upload")
    if request.method == "POST":
        form = DeliveryForm(request.POST)
        if form.is_valid():
            request.session["shipping"] = form.cleaned_data
            return redirect("studio:checkout")
    else:
        form = DeliveryForm()
    msg = request.session.pop("delivery_msg", None)
    from . import currency as fx
    return render(request, "studio/delivery.html", {"form": form, "order": order, "msg": msg,
                                                    "amt": fx.order_amounts(order, fx.for_request(request))})


def _discount_for(order, applied):
    gross = order["price"]
    if applied and applied.get("code"):
        rate = float(applied.get("rate", _rate()))
        amount = round(gross * rate, 2)
        return ({"code": applied["code"], "rate": rate, "percent": int(round(rate * 100)),
                 "amount": amount}, round(gross - amount, 2))
    return None, gross


def redeem(request):
    """Scan du QR : pre-remplit le champ code au paiement (sans l'appliquer)."""
    code = discounts.norm(request.GET.get("id") or request.GET.get("code"))
    if code:
        request.session["prefill_code"] = code
    if request.session.get("order") and request.session.get("shipping"):
        return redirect("studio:checkout")
    request.session["discount_msg"] = {"kind": "ok",
                                        "text": f"Code {code} pret : il sera propose au paiement."}
    return redirect("studio:upload")


def checkout(request):
    order = request.session.get("order")
    shipping = request.session.get("shipping")
    if not (order and shipping):
        return redirect("studio:upload")
    if request.method == "POST" and "code" in request.POST:
        code = discounts.norm(request.POST.get("code"))
        if discounts.is_redeemable(code):
            request.session["applied_discount"] = {"code": code, "rate": discounts.rate_for(code)}
            request.session["discount_msg"] = {"kind": "ok", "text": f"Code {code} applique."}
            request.session.pop("prefill_code", None)
        else:
            st = discounts.status(code)
            request.session.pop("applied_discount", None)
            txt = {"unknown": "Code inconnu.", "inactive": "Code desactive."}.get(st, "Code epuise ou deja utilise.")
            request.session["discount_msg"] = {"kind": "err", "text": txt}
        return redirect("studio:checkout")
    applied = request.session.get("applied_discount")
    discount, total = _discount_for(order, applied)
    msg = request.session.pop("discount_msg", None)
    pr = Pricing.get()
    from . import currency as fx
    return render(request, "studio/checkout.html", {
        "order": order, "shipping": shipping, "discount": discount, "total": total,
        "amt": fx.order_amounts({**order, "discount": discount}, fx.for_request(request)),
        "msg": msg, "stripe": payments.stripe_live(),
        "prefill_code": request.session.get("prefill_code", ""),
        "delivery_min": pr.delivery_days_min, "delivery_max": pr.delivery_days_max,
    })


# ---------------- Fulfilment ----------------
def _poster_svg(colors):
    """SVG legende palette (swatches numerotes + hex) pour le fournisseur."""
    cols = colors or []
    cw, ch, per = 60, 60, 6
    rows = (len(cols) + per - 1) // per or 1
    W, H = per * cw + 20, rows * (ch + 22) + 40
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%dmm" height="%dmm" viewBox="0 0 %d %d">' % (W, H, W, H)]
    out.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    out.append('<text x="10" y="24" font-family="Arial" font-size="18" font-weight="bold" fill="#12224f">PaintIt , Palette</text>')
    for i, c in enumerate(cols):
        r, col = divmod(i, per)
        x, y = 10 + col * cw, 36 + r * (ch + 22)
        hexv = c.get("hex", "#cccccc")
        out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="6" fill="%s" stroke="#e2e8f2"/>' % (x, y, cw - 8, ch - 8, hexv))
        out.append('<text x="%d" y="%d" font-family="Arial" font-size="16" font-weight="bold" fill="#12224f">%s</text>' % (x + 4, y + 20, c.get("number", "")))
        out.append('<text x="%d" y="%d" font-family="Arial" font-size="9" fill="#5b647a">%s</text>' % (x, y + ch + 6, hexv))
    out.append('</svg>')
    return "\n".join(out)


def _build_supplier_assets(o, shipping):
    """Genere order.json (consignee/order/couleurs) + poster.svg/.tiff pour le fournisseur."""
    import json as _json
    from django.utils import timezone
    uid = o.get("uid")
    if not uid:
        return
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    if not os.path.isdir(d):
        return
    colors = []
    cj = os.path.join(d, "%s_colors.json" % uid)
    if os.path.exists(cj):
        try:
            colors = _json.load(open(cj, encoding="utf-8"))
        except Exception:
            colors = []
    # order.json decompose
    doc = {
        "consignee_information": {k: shipping.get(k, "") for k in
            ["full_name", "email", "phone_code", "phone", "address1", "address2",
             "postal_code", "city", "country"]},
        "order_information": {
            "uid": uid, "format": o.get("format_label"),
            "width_cm": o.get("width_cm"), "height_cm": o.get("height_cm"),
            "orientation": o.get("orientation"), "colors_count": len(colors) or o.get("colors"),
            "brushes": bool(o.get("brushes")), "total_eur": o.get("total"),
            "order_date": timezone.now().isoformat()},
        "color_specifications": [
            {"number": c.get("number"), "hex": c.get("hex"), "rgb": c.get("rgb")} for c in colors],
    }
    try:
        with open(os.path.join(d, "%s_order.json" % uid), "w", encoding="utf-8") as f:
            _json.dump(doc, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("order.json %s", uid)
    # poster.svg + poster.tiff
    try:
        svg = _poster_svg(colors)
        with open(os.path.join(d, "%s_poster.svg" % uid), "w", encoding="utf-8") as f:
            f.write(svg)
        try:
            import cairosvg
            from PIL import Image
            from io import BytesIO
            png = cairosvg.svg2png(bytestring=svg.encode("utf-8"), dpi=300, background_color="#ffffff")
            Image.open(BytesIO(png)).convert("RGB").save(
                os.path.join(d, "%s_poster.tiff" % uid), format="TIFF", compression="tiff_lzw", dpi=(300, 300))
        except Exception:
            pass
    except Exception:
        logger.exception("poster %s", uid)


def _notify_supplier(o, shipping, sup=None, user=""):
    """Transmet la commande au fournisseur (voir studio/integrations.py). Ne casse jamais la commande."""
    try:
        from . import integrations
        return integrations.dispatch(o, shipping, sup=sup, user=user)
    except Exception:
        logger.exception("Transmission fournisseur %s", o.get("uid"))
        return False


def _safe_export_tiff(uid):
    try:
        from .pipeline import export_tiff
        export_tiff(uid)
    except Exception:
        logger.exception("Export TIFF apercu %s", uid)


def _reformat_for_supplier(o):
    """Apres paiement, avant l'envoi fournisseur : regenere la toile aux dimensions
    du format commande (le poster, order.json et les TIFF sont produits ensuite, d'apres elle)."""
    from .pipeline import source_file, generate, export_tiff
    uid = o.get("uid")
    if not uid:
        return
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    src = source_file(d, uid)
    try:
        if src and os.path.exists(src):
            from .forms import DENSITY_LEVELS
            dens, det = DENSITY_LEVELS.get(o.get("difficulty"), DENSITY_LEVELS["auto"])   # densite choisie par le client
            generate(src, o.get("colors", 24), o.get("width_cm", 40), o.get("height_cm", 50),
                     uid=uid, source_name=os.path.basename(src), density=dens, detail=det)
    except Exception:
        logger.exception("Reformatage toile %s", uid)


def _upsert_order(o, shipping, status, supplier_ref=None):
    d = o.get("discount")
    Order.objects.update_or_create(uid=o["uid"], defaults={
        "status": status, "lang": o.get("lang", "fr"),
        "format_label": o["format_label"], "orientation": o["orientation"],
        "width_cm": o["width_cm"], "height_cm": o["height_cm"], "colors": o["colors"],
        "difficulty": o.get("difficulty", "auto"),
        "brushes": bool(o.get("brushes")), "brushes_amount": o.get("brushes_amount", 0.0),
        "framed": bool(o.get("framed")),
        "currency": o.get("currency") or "EUR", "amounts": o.get("amounts") or {},
        "price": o["price"], "cost": supplier.estimate_cost(o, shipping),
        "discount_code": d["code"] if d else None, "discount_amount": d["amount"] if d else 0.0,
        "total": o.get("total", o["price"]),
        "customer_name": shipping["full_name"], "customer_email": shipping["email"],
        "phone_code": shipping.get("phone_code", ""), "phone": shipping.get("phone", ""),
        "address1": shipping.get("address1", ""), "address2": shipping.get("address2", ""),
        "postal_code": shipping.get("postal_code", ""), "city": shipping.get("city", ""),
        "country": shipping.get("country", ""), "supplier_ref": supplier_ref,
        **({"ad_ref": o["ad_ref"][:24]} if o.get("ad_ref") else {}),
    })


def _fulfill(order, shipping):
    discount = order.get("discount")
    total = order.get("total", order["price"])
    if discount and not discounts.redeem(discount["code"], by=order["uid"]):
        discount, total = None, order["price"]
    o = dict(order); o["discount"] = discount; o["total"] = total
    o["cost"] = supplier.estimate_cost(o)
    manifest = fulfillment.build(o, shipping)
    supplier_result = supplier.place_order(o, shipping, manifest)
    discounts.issue(o["uid"])
    discounts.issue_referral(security.referral_code_for(o["uid"]))
    _upsert_order(o, shipping, status=Order.FULFILLED, supplier_ref=supplier_result.get("supplier_ref"))
    try:
        Order.objects.get(uid=o["uid"]).assign_invoice_number()
    except Exception:
        logger.exception("Numero de facture %s", o["uid"])
    # Lourd (regen toile au bon format + TIFF + notif fournisseur + email) -> tache de fond,
    # APRES paiement, pour repondre tout de suite (pas de lag au checkout).
    threading.Thread(target=_post_order_async, args=(dict(o), dict(shipping)), daemon=True).start()
    return o, manifest, supplier_result


SUPPLIER_FILES = [("template_pdf", "{uid}_template.pdf", "Toile numérotée PDF vectoriel (impression)"),
                  ("template_tiff", "{uid}_template.tiff", "Toile numérotée TIFF 600 dpi"),
                  ("template_svg", "{uid}_template.svg", "Toile numérotée SVG (source)"),
                  ("poster", "{uid}_poster.png", "Poster (notice + palette + QR)"),
                  ("supplier_json", "{uid}_supplier.json", "Fiche fournisseur JSON (sans prix)"),
                  ("supplier_txt", "{uid}_supplier.txt", "Fiche fournisseur texte (EN/中文, sans prix)"),
                  ("supplier_jpg", "{uid}_supplier_colors.jpg", "Tableau des couleurs (JPG)"),
                  ("order_json", "{uid}_order.json", "Bon de commande interne (order.json)")]


def supplier_files_status(uid):
    import json as _json
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)

    def ok(name):
        p = os.path.join(d, name)
        if not os.path.exists(p):
            return False
        if name.endswith(".json"):   # une fiche sans couleurs est inutilisable par le fournisseur
            try:
                return bool(_json.load(open(p, encoding="utf-8")).get("color_specifications"))
            except ValueError:
                return False
        return True
    return [{"key": k, "name": n.format(uid=uid), "label": l, "ok": ok(n.format(uid=uid))}
            for k, n, l in SUPPLIER_FILES]


def ensure_supplier_files(o, shipping, force=False, user="auto"):
    """Produit le dossier fournisseur : poster + order.json (d'apres la toile FINALE) puis TIFF HD.
    Chaque etape est independante et journalisee. -> liste des fichiers manquants restants."""
    from .pipeline import export_tiff
    uid = o.get("uid")
    st = {f["key"]: f["ok"] for f in supplier_files_status(uid)}
    errors = []
    if st["supplier_json"] and not (st["supplier_txt"] and st["supplier_jpg"]) and not force:
        fulfillment.write_supplier_text(uid)          # commandes anterieures : fiche texte + tableau JPG deduits du JSON
        st["supplier_txt"] = st["supplier_jpg"] = True
    if force or not (st["poster"] and st["order_json"] and st["supplier_json"]):
        try:
            fulfillment.build(o, shipping)
        except Exception as exc:
            logger.exception("Poster/order.json %s", uid); errors.append("poster/order.json : %s" % exc)
    if force or not (st["template_tiff"] and st["template_pdf"]):
        try:
            export_tiff(uid)
        except Exception as exc:
            logger.exception("TIFF %s", uid); errors.append("TIFF : %s" % exc)
    missing = [f["label"] for f in supplier_files_status(uid) if not f["ok"]]
    row = Order.objects.filter(uid=uid).first()
    if row:
        OrderEvent.objects.create(order=row, kind="action", user=user, text=(
            "Dossier fournisseur prêt (PDF vectoriel, TIFF 600 dpi, poster, fiches JSON)" if not missing
            else "Dossier fournisseur incomplet : manque %s%s" % (", ".join(missing),
                                                                 (" · " + " ; ".join(errors)) if errors else ""))[:300])
    return missing


def _post_order_async(o, shipping):
    """Traitement post-paiement en arriere-plan, etapes INDEPENDANTES (une erreur n'empeche pas la suite) :
    1. toile regeneree aux dimensions commandees ; 2. poster + order.json + TIFF d'apres cette toile ;
    3. transmission fournisseur ; 4. e-mail de confirmation."""
    try:
        _reformat_for_supplier(o)
    except BaseException:
        logger.exception("Reformat async %s", o.get("uid"))
    try:
        ensure_supplier_files(o, shipping, force=True)
    except BaseException:
        logger.exception("Dossier fournisseur %s", o.get("uid"))
    try:
        from .models import Supplier
        sup = Supplier.for_checkout("kit")
        if sup and sup.auto_dispatch:
            _notify_supplier(o, shipping, sup=sup, user="auto")
        elif sup:   # validation manuelle : la commande reste "payee" -> Centre d'actions
            Order.objects.filter(uid=o.get("uid"), status=Order.FULFILLED).update(status=Order.PAID)
    except BaseException:
        logger.exception("Notify async %s", o.get("uid"))
    try:
        emails.send_order_confirmation(o, shipping)
    except BaseException:
        logger.exception("Email async %s", o.get("uid"))


def place_order(request):
    if request.method != "POST":
        return redirect("studio:checkout")
    order = request.session.get("order")
    shipping = request.session.get("shipping")
    if not (order and shipping):
        return redirect("studio:upload")
    if address.validate(shipping):
        request.session["delivery_msg"] = {"kind": "err", "text": "Adresse invalide, merci de la corriger."}
        return redirect("studio:delivery")

    applied = request.session.get("applied_discount")
    discount, total = _discount_for(order, applied)
    if not request.POST.get("accept_cgv"):
        request.session["discount_msg"] = {"kind": "err", "text": _("Merci d'accepter les conditions générales de vente.")}
        return redirect("studio:checkout")
    o = dict(order); o["discount"] = discount; o["total"] = total
    from . import currency as fx       # montants dans la devise du client = ce que Stripe debite
    o["currency"] = fx.for_request(request)
    o["amounts"] = fx.order_amounts(o, o["currency"])
    o["lang"] = (get_language() or o.get("lang", "fr"))[:2]   # langue du process (achat)
    o["ad_ref"] = request.session.get("ad_ref", "")             # A/B marketing : pub d'origine

    if payments.stripe_live():
        _upsert_order(o, shipping, status=Order.PENDING)
        try:
            url = payments.create_checkout_session(o, shipping, request)
        except Exception as exc:
            return render(request, "studio/checkout.html", {
                "order": order, "shipping": shipping, "discount": discount, "total": total, "amt": o["amounts"],
                "stripe": True, "msg": {"kind": "err", "text": f"Stripe indisponible : {exc}"}})
        return redirect(url)

    if not payments.demo_allowed():   # prod sans Stripe : on ne livre JAMAIS sans paiement
        logger.error("Commande refusee : Stripe non configure (STRIPE_SECRET_KEY)")
        return render(request, "studio/checkout.html", {
            "order": order, "shipping": shipping, "discount": discount, "total": total, "amt": o["amounts"],
            "stripe": False, "msg": {"kind": "err", "text": _("Paiement momentanément indisponible, merci de réessayer plus tard.")}})
    # Demo (dev uniquement) : pas de paiement reel -> on honore la commande maintenant.
    o, manifest, supplier_result = _fulfill(o, shipping)
    request.session.pop("applied_discount", None)
    _record_gift(o, shipping)
    request.session["verified_email"] = shipping.get("email", "")   # galerie auto apres achat
    _rc = security.referral_code_for(o["uid"])
    return render(request, "studio/confirmation.html", {
        "referral_code": _rc, "referral_percent": int(round(Pricing.get().referral_rate*100)),
        "order": o, "shipping": shipping, "discount": o["discount"], "total": o["total"],
        "amt": o.get("amounts") or {}, "supplier": supplier_result, "manifest": manifest, "uid": o["uid"],
    })


def _record_gift(o, shipping):
    try:
        DigitalCanvas.objects.update_or_create(
            email=(shipping.get("email") or ""), uid=o["uid"],
            defaults=dict(colors=o["colors"], orientation=o["orientation"],
                          width_cm=o["width_cm"], height_cm=o["height_cm"], source="gift"))
    except Exception:
        pass


def _shipping_from_row(r):
    return {"full_name": r.customer_name, "email": r.customer_email,
            "phone_code": r.phone_code, "phone": r.phone, "address1": r.address1,
            "address2": r.address2, "postal_code": r.postal_code, "city": r.city,
            "country": r.country}


def _order_from_row(r):
    discount = None
    if r.discount_code and r.price:
        rate = r.discount_amount / r.price
        discount = {"code": r.discount_code, "rate": rate,
                    "percent": int(round(rate * 100)), "amount": r.discount_amount}
    canvas = round(r.price - r.brushes_amount, 2)
    return {"uid": r.uid, "format_label": r.format_label, "orientation": r.orientation,
            "width_cm": r.width_cm, "height_cm": r.height_cm, "colors": r.colors,
            "difficulty": r.difficulty, "brushes": r.brushes, "brushes_amount": r.brushes_amount,
            "framed": r.framed, "canvas_price": canvas, "price": r.price, "discount": discount, "total": r.total,
            "lang": r.lang, "currency": r.currency or "EUR", "amounts": r.amounts or {}}


def _ensure_fulfilled(uid, why=""):
    """Honore une commande PAYEE une seule fois : seule une commande "en attente de paiement" est
    traitee, et la bascule est atomique (webhook et page de retour peuvent arriver en meme temps)."""
    r = Order.objects.filter(uid=uid).first()
    if not r:
        return None
    if Order.objects.filter(pk=r.pk, status=Order.PENDING).update(status=Order.PAID, status_changed_at=timezone.now()):
        from .models import OrderEvent
        OrderEvent.objects.create(order=r, kind="status", status=Order.PAID, text=("Paiement confirme " + why).strip())
        r.refresh_from_db()
        _fulfill(_order_from_row(r), _shipping_from_row(r))
        r.refresh_from_db()
    return r


def pay_success(request):
    uid = request.GET.get("uid")
    sid = request.GET.get("sid", "")
    r = Order.objects.filter(uid=uid).first() if uid else None
    if not r:
        return redirect("studio:home")
    sess = payments.paid_session(sid, uid=uid, kind="kit")
    mine = (request.session.get("order") or {}).get("uid") == uid
    if sess:
        r = _ensure_fulfilled(uid, "(retour Stripe)")
    elif not (mine and r.status in Order.PAID_STATUSES):
        # Pas de preuve de paiement : on ne livre rien et on n'expose pas la commande.
        return redirect("studio:checkout" if mine else "studio:home")
    request.session.pop("applied_discount", None)
    o = _order_from_row(r)
    ship = _shipping_from_row(r)
    _record_gift(o, ship)
    request.session["verified_email"] = ship.get("email", "")   # galerie auto apres achat
    _rc = security.referral_code_for(o["uid"])
    return render(request, "studio/confirmation.html", {
        "referral_code": _rc, "referral_percent": int(round(Pricing.get().referral_rate*100)),
        "order": o, "shipping": _shipping_from_row(r), "discount": o["discount"],
        "total": o["total"], "amt": o.get("amounts") or {}, "supplier": {"status": "ok", "supplier_ref": r.supplier_ref},
        "manifest": {"files": ["order.json", f"{uid}_template.svg", f"{uid}_poster.png"]},
        "uid": uid})


@csrf_exempt
def stripe_webhook(request):
    try:
        event = payments.verify_webhook(request.body, request.headers.get("Stripe-Signature", ""))
    except Exception:
        return HttpResponse(status=400)
    etype = event.get("type")
    obj = event["data"]["object"]
    meta = obj.get("metadata", {}) or {}
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") != "paid":      # paiement differe : on attend async_payment_succeeded
            return HttpResponse(status=200)
        uid = meta.get("uid") or obj.get("client_reference_id")
        kind = meta.get("kind", "kit")
        from . import ledger
        ledger.record_from_session(obj)
        buyer = meta.get("email") or _buyer_email(None, obj)
        if uid and kind == "digital":
            if _record_digital(uid, buyer, {}) and buyer:
                _digital_receipt(buyer, uid, "", obj, meta.get("lang"))
        elif uid and kind == "gallery":
            m = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
            if m and buyer and _grant_gallery_to(m, buyer):
                _digital_receipt(buyer, uid, m.title, obj, meta.get("lang"))
        elif uid:
            r = Order.objects.filter(uid=uid).first()
            paid = (obj.get("amount_total") or 0) / 100.0
            cur = (obj.get("currency") or "eur").upper()
            due = float((r.amounts or {}).get("total") or r.total or 0) if r and r.currency == cur and cur != "EUR" \
                else float(r.total or 0) if r else 0.0
            if r and r.status == Order.PENDING and (paid + 0.01 < due or cur != (r.currency or "EUR")):
                logger.error("Montant Stripe %.2f %s < total commande %.2f %s (%s)", paid, cur, due, r.currency, uid)
                _order_note(r, "ALERTE : montant paye %.2f %s < total %.2f %s, non traitee" % (paid, cur, due, r.currency))
            else:
                _ensure_fulfilled(uid, "(webhook Stripe)")
    elif etype in ("charge.refunded", "charge.dispute.created"):
        uid = meta.get("uid")
        if not uid and obj.get("payment_intent"):
            try:
                import stripe as _stripe
                _stripe.api_key = settings.STRIPE_SECRET_KEY
                uid = (payments.plain(_stripe.PaymentIntent.retrieve(obj["payment_intent"])).get("metadata") or {}).get("uid")
            except Exception:
                logger.exception("Stripe %s", etype)
        r = Order.objects.filter(uid=uid).first() if uid else None
        if etype == "charge.refunded":
            from . import ledger
            kind = meta.get("kind") or ("kit" if r else "digital")
            refunds = ((obj.get("refunds") or {}).get("data")) or []
            from .currency import to_eur
            cur = obj.get("currency") or "eur"
            for rf in refunds:
                ledger.record_refund(rf.get("id"), to_eur((rf.get("amount") or 0) / 100.0, cur), uid=uid or "", kind=kind,
                                     payment_intent=obj.get("payment_intent") or "")
            if not refunds:
                ledger.record_refund("%s-%s" % (obj.get("id"), obj.get("amount_refunded")),
                                     to_eur((obj.get("amount_refunded") or 0) / 100.0, cur), uid=uid or "", kind=kind,
                                     payment_intent=obj.get("payment_intent") or "")
        if r:
            label = "Remboursement Stripe" if etype == "charge.refunded" else "LITIGE Stripe ouvert"
            _order_note(r, "%s : %.2f %s" % (label, (obj.get("amount_refunded") or obj.get("amount") or 0) / 100.0,
                                              (obj.get("currency") or "eur").upper()))
    return HttpResponse(status=200)


def _order_note(r, text):
    from .models import OrderEvent
    OrderEvent.objects.create(order=r, kind="note", text=text[:300], user="stripe")
    Order.objects.filter(pk=r.pk).update(notes=((r.notes or "") + "\n" + text).strip())


def receipt_pdf(request, uid):
    order = request.session.get("order")
    r = Order.objects.filter(uid=uid).first()
    if not r or not (order and order.get("uid") == uid):
        raise Http404
    pdf = receipts.build_receipt(r)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="PaintIt-{uid}.pdf"'
    return resp


# ---------------- Contact ----------------
def privacy(request):
    from .models import CompanyInfo
    return render(request, "studio/privacy.html", {"info": CompanyInfo.get()})


def faq_page(request):
    from . import faq, seo
    from django.utils.translation import gettext as _t
    items = faq.faq_items()
    return render(request, "studio/faq.html", {
        "items": items, "ld_faq": seo.dump(seo.faq(items)),
        "ld_crumb": seo.dump(seo.breadcrumb((_t("Accueil"), "/"), (_t("Questions fréquentes"), request.path)))})


def legal(request):
    from .models import CompanyInfo
    return render(request, "studio/legal.html", {"info": CompanyInfo.get()})


def cgv(request):
    from .models import CompanyInfo
    return render(request, "studio/cgv.html", {"info": CompanyInfo.get()})


def contact(request):
    sent = False
    attach_error = None
    if request.method == "POST" and (request.POST.get("website")      # champ piege invisible : robot
                                     or security.rate_limited("contact:" + security.client_ip(request), 5, 3600)):
        return render(request, "studio/contact.html", {"form": ContactForm(), "sent": True})
    if request.method == "POST":
        form = ContactForm(request.POST)
        files = request.FILES.getlist("attachments")
        allowed = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}
        attachments = []
        if len(files) > 5:
            attach_error = "5 fichiers maximum."
        else:
            total = 0
            for f in files:
                if f.content_type not in allowed:
                    attach_error = "Formats acceptes : images ou PDF."
                    break
                if f.size > 8 * 1024 * 1024:
                    attach_error = "Chaque fichier doit faire moins de 8 Mo."
                    break
                total += f.size
                attachments.append((f.name, f.read(), f.content_type))
            if total > 20 * 1024 * 1024:
                attach_error = "Total des pieces jointes trop lourd (20 Mo max)."
        if form.is_valid() and not attach_error:
            cd = form.cleaned_data
            from .models import ContactMessage, ContactAttachment
            from django.core.files.base import ContentFile
            msg = ContactMessage.objects.create(name=cd["name"], email=cd["email"],
                                                subject=cd["subject"], message=cd["message"])
            for (fname, data, ctype) in attachments:
                try:
                    ContactAttachment.objects.create(
                        message=msg, original_name=fname, content_type=ctype or "",
                        file=ContentFile(data, name=fname))
                except Exception:
                    pass
            emails.send_contact(attachments=attachments, **cd)
            sent = True
            form = ContactForm()
    else:
        form = ContactForm()
    return render(request, "studio/contact.html",
                  {"form": form, "sent": sent, "attach_error": attach_error})


# ---------------- Tour de controle (dashboard) ----------------
@staff_member_required
def finance_dashboard(request):
    """Ancienne 'Tour de controle' : fusionnee dans le Hub ERP (definitions de CA unifiees)."""
    return redirect("studio:admin_hub")


def _geo_facts():
    """Faits verifiables (prix, delais, couleurs, origine) pour les moteurs IA (GEO) et le SEO."""
    from .models import KitFormat, CompanyInfo
    from . import faq as _faq
    p = Pricing.get()
    fmts = list(KitFormat.objects.filter(available=True))
    prices = [float(f.price) for f in fmts] or [p.format_price("40x50")]
    return {
        "site": settings.SITE_URL, "origin": CompanyInfo.get().production_origin or "",
        "formats": [f.label for f in fmts], "price_from": min(prices),
        "colors": p.available_colors(), "dmin": p.delivery_days_min, "dmax": p.delivery_days_max,
        "faq": _faq.faq_items("fr"),
    }


def llm_txt(request):
    from django.http import HttpResponse
    f = _geo_facts()
    site = f["site"]
    made = " Les kits sont fabriqués à la demande par nos ateliers partenaires, audités selon nos exigences de qualité."
    lines = [
        "# PaintIt : peinture par numéros personnalisée à partir de votre photo",
        "",
        "> PaintIt transforme une photo personnelle en kit de peinture par numéros unique. "
        "Le client teste gratuitement avant d'acheter (aperçu + jeu en ligne), puis reçoit chez lui "
        "un kit de qualité livré gratuitement." + made,
        "",
        "## Positionnement",
        "- 100 % personnalisé : chaque toile est calculée pour la photo du client (pas de catalogue de modèles).",
        "- Testez avant d'acheter : aperçu gratuit en moins d'une minute, jeu de peinture en ligne gratuit, prix affichés d'emblée.",
        "- Algorithme de précision : zones réellement peignables, numéros lisibles, couleurs choisies parmi de vraies peintures disponibles chez l'atelier partenaire.",
        "- Qualité premium : kits fabriqués à la demande par nos ateliers partenaires, audités selon nos exigences de qualité, livraison offerte.",
        "- Accessible à tous : dès 4 ans en 12 couleurs (enfants, seniors) ; 24 couleurs équilibré ; 36 couleurs et plus pour les passionnés.",
        "",
        "## Faits",
        "- Formats : %s." % ", ".join(f["formats"]),
        "- Nombre de couleurs : %s." % ", ".join(str(c) for c in f["colors"]),
        "- Prix : à partir de %.2f EUR, livraison offerte." % f["price_from"],
        "- Délai de livraison : %d à %d jours ouvrés (fabrication à la demande)." % (f["dmin"], f["dmax"]),
        "- Contenu du kit : toile numérotée, pots de peinture numérotés, poster de référence ; pinceaux en option.",
        "- Paiement sécurisé par carte (Stripe). Langues : français, anglais, allemand, espagnol, italien.",
        "",
        "## Cas d'usage",
        "Cadeau personnalisé (anniversaire, fête des mères/pères, Noël, naissance, mariage), portrait d'enfant ou d'animal, "
        "souvenir de voyage, activité créative relaxante en famille, activité pour seniors, décoration murale unique.",
        "",
        "## Questions fréquentes",
    ]
    lines += ["Q: %s\nR: %s" % (it["q"], it["a"]) for it in f["faq"]]
    from . import landings
    lines += ["", "## Ce qui distingue PaintIt",
              "- Le client voit sa toile exacte, son niveau de difficulté et son prix avant de payer.",
              "- Le client peut peindre sa toile en ligne gratuitement pour juger du niveau de difficulté.",
              "- Trois niveaux de détail (Facile, Moyen, Expert) et de 12 à 48 couleurs, réglables avant l'achat.",
              "- Couleurs calculées dans l'espace perceptuel CIELAB puis rapprochées des peintures réellement disponibles.",
              "", "## Pages thématiques"]
    for lang in landings.PAGES:
        for t in landings.TOPICS:
            if t in landings.PAGES[lang]:
                lines.append("- [%s] %s : %s%s" % (lang, landings.PAGES[lang][t]["title"].format(**landings.facts(lang)),
                                                   site, landings.url(lang, t)))
    lines += ["", "## In English",
              "PaintIt (paintit.click) turns any photo into a custom paint-by-numbers kit. Free preview and free online "
              "painting game before buying, 3 detail levels, 12 to 48 colours, complete kit with free shipping. "
              "Available in English, French, German, Spanish and Italian."]
    co = CompanyInfo.get()
    ident = [("Raison sociale", " ".join(x for x in (co.legal_name, co.legal_form) if x)), ("SIRET", co.siret),
             ("RCS", co.rcs), ("TVA", co.vat_number), ("Siège", " ".join(co.address.split())),
             ("Contact", co.email)]
    lines += ["", "## Société exploitante"] + ["- %s : %s" % (k, v) for k, v in ident if v] + [
              "- Mentions légales : %s/mentions-legales/" % site]
    lines += ["", "## Liens",
              "- Créer une toile : %s/create/" % site, "- Tester le jeu : %s/play/" % site,
              "- FAQ : %s/faq/" % site, "- Contact : %s/contact/" % site,
              ] + ["- %s : %s" % (n, u) for n, u in CompanyInfo.get().socials()] + [
              "- Données structurées : %s/llm.json" % site, "- Plan du site : %s/sitemap.xml" % site]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")


def llm_json(request):
    from django.http import JsonResponse
    f = _geo_facts()
    site = f["site"]
    data = {
        "name": "PaintIt",
        "tagline": "Votre souvenir. Votre chef-d’œuvre.",
        "description": "Peinture par numéros 100 % personnalisée à partir de votre photo : testez gratuitement "
                       "avant d'acheter, algorithme de précision, kit de qualité fabriqué à la demande par nos ateliers partenaires audités et livré gratuitement.",
        "positioning": ["personnalisation totale (pas de catalogue)", "tester avant d'acheter (aperçu et jeu gratuits)",
                        "transparence des prix", "algorithme de précision (vraies peintures disponibles)",
                        "qualité premium, fabrication à la demande par des ateliers partenaires audités"],
        "audience": {"min_age": 4, "note": "12 couleurs : très accessible pour les enfants et les seniors"},
        "offer": {"formats": f["formats"], "colors": f["colors"], "price_from_eur": round(f["price_from"], 2),
                  "shipping": "offerte", "delivery_days": [f["dmin"], f["dmax"]],
                  "kit": ["toile numérotée", "pots de peinture numérotés", "poster de référence", "pinceaux (option)"]},
        "use_cases": ["cadeau personnalisé", "portrait d'enfant ou d'animal", "souvenir de voyage",
                      "activité créative en famille", "activité pour seniors", "décoration murale"],
        "languages": ["fr", "en", "de", "es", "it"],
        "faq": f["faq"],
        "links": {"home": site + "/", "create": site + "/create/", "play": site + "/play/",
                  "faq": site + "/faq/", "contact": site + "/contact/", "sitemap": site + "/sitemap.xml",
                  "social": {n.lower(): u for n, u in CompanyInfo.get().socials()},
                  "legal": site + "/mentions-legales/"},
    }
    co = CompanyInfo.get()
    data["company"] = {k: v for k, v in (("legal_name", co.legal_name), ("legal_form", co.legal_form),
                                         ("siret", co.siret), ("rcs", co.rcs), ("vat_id", co.vat_number),
                                         ("address", " ".join(co.address.split())), ("email", co.email)) if v}
    return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})


def merchant_feed(request):
    """Flux produits Google Merchant Center (RSS 2.0 + g:). Une fiche par langue au prix « dès » affiché
    sur la page d'atterrissage (Google rejette tout écart de prix entre le flux et la page)."""
    from xml.sax.saxutils import escape
    from . import landings
    site = settings.SITE_URL
    p = Pricing.get()
    days = (p.delivery_days_min, p.delivery_days_max)
    countries = {"fr": ["FR", "BE", "CH", "LU"], "en": ["GB", "IE"], "de": ["DE", "AT"], "es": ["ES"], "it": ["IT"]}
    items = []
    for lang in [l for l, _ in settings.LANGUAGES if l in landings.PAGES]:
        page = landings.PAGES[lang].get("custom")
        if not page:
            continue
        fx = landings.facts(lang)
        if not fx["price"]:
            continue
        price = fx["price"].replace(",", ".")
        ship = "".join("<g:shipping><g:country>%s</g:country><g:price>0.00 EUR</g:price>"
                       "<g:min_handling_time>1</g:min_handling_time><g:max_handling_time>3</g:max_handling_time>"
                       "<g:min_transit_time>%d</g:min_transit_time><g:max_transit_time>%d</g:max_transit_time>"
                       "</g:shipping>" % (c, days[0], days[1]) for c in countries.get(lang, []))
        items.append(
            "<item><g:id>PAINTIT-CUSTOM-%s</g:id><g:item_group_id>PAINTIT-CUSTOM</g:item_group_id>"
            "<title>%s</title><description>%s</description><link>%s</link>"
            "<g:image_link>%s</g:image_link><g:additional_image_link>%s</g:additional_image_link>"
            "<g:additional_image_link>%s</g:additional_image_link>"
            "<g:price>%.2f EUR</g:price><g:availability>in_stock</g:availability><g:condition>new</g:condition>"
            "<g:brand>PaintIt</g:brand><g:identifier_exists>no</g:identifier_exists>"
            "<g:product_type>Arts &amp; Crafts &gt; Painting Kits &gt; Custom Paint by Numbers</g:product_type>"
            "<g:is_bundle>yes</g:is_bundle>%s</item>"
            % (lang.upper(), escape(page["title"].format(**fx)), escape(page["desc"].format(**fx)),
               escape(site + landings.url(lang, "custom")), site + "/static/studio/img/og.jpg",
               site + "/static/studio/img/kit_poster.png", site + "/static/studio/img/kit_pots.jpg",
               float(price), ship))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">'
           "<channel><title>PaintIt</title><link>%s/</link><description>Peinture par numéros personnalisée</description>"
           "%s</channel></rss>\n" % (site, "".join(items)))
    return HttpResponse(xml, content_type="application/xml; charset=utf-8")

def sitemap_xml(request):
    from django.http import HttpResponse
    from django.urls import translate_url
    from .models import DigitalCanvas
    site = settings.SITE_URL
    # pages publiques uniquement (les toiles /play/<uid>/ ne sont pas indexees)
    paths = ["/", "/create/", "/play/", "/faq/", "/contact/", "/cgv/", "/mentions-legales/", "/privacy/"]
    langs = [c for c, _ in settings.LANGUAGES]
    # ordre impose par le schema sitemaps.org : loc, lastmod, changefreq, puis les extensions (xhtml:link)
    today = timezone.localdate().isoformat()
    x = ['<?xml version="1.0" encoding="UTF-8"?>',
         '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
         'xmlns:xhtml="http://www.w3.org/1999/xhtml">']
    for p in paths:
        variants = {}
        for c in langs:
            try:
                variants[c] = site + translate_url(p, c)
            except Exception:
                variants[c] = site + p
        alts = "".join(
            '<xhtml:link rel="alternate" hreflang="%s" href="%s"/>' % (c, variants[c]) for c in langs)
        alts += '<xhtml:link rel="alternate" hreflang="x-default" href="%s"/>' % (site + p)
        for c in langs:
            x.append("<url><loc>%s</loc><lastmod>%s</lastmod><changefreq>weekly</changefreq>%s</url>" % (variants[c], today, alts))
    from . import landings
    for topic in landings.TOPICS:
        alt = {l: site + landings.url(l, topic) for l in landings.PAGES if topic in landings.PAGES[l]}
        links = "".join('<xhtml:link rel="alternate" hreflang="%s" href="%s"/>' % (l, u) for l, u in alt.items())
        links += '<xhtml:link rel="alternate" hreflang="x-default" href="%s"/>' % alt.get(settings.LANGUAGE_CODE, "")
        for u in alt.values():
            x.append("<url><loc>%s</loc><lastmod>%s</lastmod><changefreq>monthly</changefreq>%s</url>" % (u, today, links))
    x.append("</urlset>")
    resp = HttpResponse("\n".join(x), content_type="application/xml; charset=utf-8")
    resp["Cache-Control"] = "public, max-age=3600"
    return resp


def landing(request, slug):
    """Page thematique (peinture par numeros adulte / enfant / animal / cadeau / guide...) dans la langue courante."""
    from . import landings, seo
    lang = (get_language() or settings.LANGUAGE_CODE)[:2]
    found_lang, topic = landings.find(slug)
    if not topic:
        raise Http404
    if found_lang != lang:          # slug d'une autre langue : page equivalente de la langue courante
        target = landings.url(lang, topic) or landings.url(found_lang, topic)
        return HttpResponsePermanentRedirect(target)
    page = landings.render_page(lang, topic)
    site = settings.SITE_URL
    request.hreflang_override = [(l, site + u) for l, u in page["alternates"]] + \
        [("x-default", site + landings.url(settings.LANGUAGE_CODE, topic))]
    home = {"fr": "Accueil", "en": "Home", "de": "Startseite", "es": "Inicio", "it": "Home"}.get(lang, "Accueil")
    ctx = {"page": page,
           "ld_faq": seo.dump(seo.faq([{"q": q, "a": a} for q, a in page["faq"]])),
           "ld_crumb": seo.dump(seo.breadcrumb((home, landings.lang_prefix(lang) + "/"),
                                               (page["h1"], landings.url(lang, topic)))),
           "ld_product": seo.dump(seo.product(request)) if topic in ("custom", "pet", "gift") else ""}
    return render(request, "studio/landing.html", ctx)


def shop_pricing(request):
    from django.contrib.admin.views.decorators import staff_member_required
    from django.contrib import messages
    from .models import PrintPricing
    if not request.user.is_staff:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())
    kit = Pricing.get(); tab = PrintPricing.get()
    from .models import Supplier, CompanyInfo, KitFormat as _KF, supplier_color_count
    sup = Supplier.for_checkout("kit")
    if request.method == "POST" and request.POST.get("op") == "align" and sup:
        if not any(sup.offer_get("formats", kf.key).get("on") for kf in _KF.objects.all()):
            messages.error(request, "L'offre de %s ne contient aucun format : saisissez-la d'abord sur sa fiche "
                                    "(Modifier → Offre & tarifs)." % sup.name)
            return redirect("studio:shop_pricing")
        # Disponibilites du Kit = ce que le fournisseur actif propose
        for kf in _KF.objects.all():
            kf.available = bool(sup.offer_get("formats", kf.key).get("on"))
            kf.frame_available = sup.goods_cost(kf.key, 24, False, True) is not None    # kit BK tarife chez lui
            kf.save(update_fields=["available", "frame_available"])
        for n in (12, 24, 36, 48):
            # 12 couleurs : proposees au client si le fournisseur fait 24 (chaque couleur en double)
            setattr(kit, "av_c%d" % n, bool(sup.offer_get("colors", supplier_color_count(n)).get("on")))
        kit.av_brushes = bool(sup.offer_get("extras", "brushes").get("on"))
        kit.save()
        messages.success(request, "Disponibilités alignées sur l'offre de %s." % sup.name)
        return redirect("studio:shop_pricing")
    if request.method == "POST":
        def f(name, d=0.0):
            try: return float(request.POST.get(name, d))
            except (TypeError, ValueError): return d
        def bb(name): return request.POST.get(name) == "on"
        from .models import KitFormat
        for kf in list(KitFormat.objects.all()):
            if request.POST.get("del_fmt_%d" % kf.id):
                kf.delete(); continue
            kf.price = f("fmt_price_%d" % kf.id, float(kf.price))
            kf.available = bb("fmt_av_%d" % kf.id)
            kf.frame_price = f("fmt_frame_%d" % kf.id, float(kf.frame_price))
            kf.frame_available = bb("fmt_frav_%d" % kf.id)
            kf.save()
        nw = request.POST.get("new_w"); nh = request.POST.get("new_h")
        if nw and nh:
            try:
                KitFormat.objects.get_or_create(
                    width_cm=int(nw), height_cm=int(nh),
                    defaults={"price": f("new_price", 34.9), "available": True,
                              "sort": KitFormat.objects.count()})
            except Exception:
                pass
        kit.c_12 = f("c_12"); kit.c_24 = f("c_24"); kit.c_36 = f("c_36"); kit.c_48 = f("c_48", kit.c_48)
        kit.av_c12 = bb("av_c12"); kit.av_c24 = bb("av_c24"); kit.av_c36 = bb("av_c36"); kit.av_c48 = bb("av_c48")
        kit.brushes_price = f("brushes_price", kit.brushes_price); kit.av_brushes = bb("av_brushes")
        kit.delivery_days_min = int(f("delivery_days_min", 5)); kit.delivery_days_max = int(f("delivery_days_max", 9))
        kit.discount_rate = f("discount_rate", kit.discount_rate)
        kit.save()
        for k in ["30x40","40x50","50x70","60x80","70x100","80x120"]:
            setattr(tab, "d_"+k, f("d_"+k, getattr(tab, "d_"+k)))
            setattr(tab, "av_"+k, bb("av_"+k))
        for k in ["toile","alu","bois"]:
            setattr(tab, "m_"+k, f("m_"+k)); setattr(tab, "av_m_"+k, bb("av_m_"+k))
        for k in ["noir","bois","blanc"]:
            setattr(tab, "f_"+k, f("f_"+k)); setattr(tab, "av_f_"+k, bb("av_f_"+k))
        tab.av_glass = bb("av_glass"); tab.glass_price = f("glass_price", tab.glass_price)
        tab.supplier_email = request.POST.get("supplier_email", "")
        tab.save()
        messages.success(request, "Tarifs enregistres.")
        return redirect("studio:shop_pricing")
    vat = float(CompanyInfo.get().vat_rate or 0)
    ht = lambda ttc: float(ttc) / (1 + vat / 100.0)
    kfs = list(_KF.objects.all())
    kit_colors = [{"key": str(n), "price": kit.color_price(n), "av": getattr(kit, "av_c%d" % n)} for n in (12, 24, 36, 48)]
    margins, mcols = [], [c for c in kit_colors if c["av"]]
    if sup:
        for kf in kfs:
            d = sup.offer_get("formats", kf.key)
            kf.sup_on, kf.sup_cost = bool(d.get("on")), d.get("cost")
            kf.sup_frame = sup.goods_cost(kf.key, 24, False, True)       # kit avec cadre (BK), 24 couleurs
        for c in kit_colors:
            d = sup.offer_get("colors", c["key"])
            c["sup_on"], c["sup_cost"] = bool(d.get("on")), d.get("cost")
        for kf in kfs:
            if not kf.available:
                continue
            cells = []
            for c in mcols:
                sale = ht(float(kf.price) + c["price"])
                cost = sup.order_cost(kf.key, int(c["key"])) if c.get("sup_on") else None
                cells.append({"sale": round(sale, 2), "cost": cost,
                              "margin": None if cost is None else round(sale - cost, 2),
                              "pct": None if cost is None or not sale else round((sale - cost) / sale * 100)})
            margins.append({"label": kf.label, "cells": cells})
    b = sup.offer_get("extras", "brushes") if sup else {}
    ctx = {
        "kit": kit, "sup": sup, "vat": vat, "margins": margins, "mcols": mcols,
        "sup_brushes": b, "sup_taxes": sup.taxes() if sup else None,
        "sup_ship": ((sup.offer or {}).get("shipping") or {}) if sup else {},
        "sup_zone": ((sup.grid_zone("FR") or {}).get("label") if sup and sup.grid() else ""),
        "kit_formats": kfs,
        "kit_colors": kit_colors,
        "tab_dims": [{"key":k,"price":getattr(tab,"d_"+k),"av":getattr(tab,"av_"+k)}
                     for k in ["30x40","40x50","50x70","60x80","70x100","80x120"]],
        "tab_materials": [{"key":"toile","label":"Toile","price":tab.m_toile,"av":tab.av_m_toile},
                          {"key":"alu","label":"Aluminium","price":tab.m_alu,"av":tab.av_m_alu},
                          {"key":"bois","label":"Bois","price":tab.m_bois,"av":tab.av_m_bois}],
        "tab_frames": [{"key":"noir","label":"Cadre noir","price":tab.f_noir,"av":tab.av_f_noir},
                       {"key":"bois","label":"Cadre bois","price":tab.f_bois,"av":tab.av_f_bois},
                       {"key":"blanc","label":"Cadre blanc","price":tab.f_blanc,"av":tab.av_f_blanc}],
        "tab": tab,
    }
    return render(request, "studio/shop_pricing.html", ctx)



# ---------------- PBN Lab (page cachee /pbn, staff-only) ----------------
@staff_member_required
def pbn_lab_page(request):
    return render(request, "pbn/index.html")


@staff_member_required
@csrf_exempt
def pbn_lab_upload(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    f = request.FILES.get("image")
    if not f:
        return JsonResponse({"error": "aucune image"}, status=400)
    import json as _json
    from . import pbn_lab
    try:
        params = _json.loads(request.POST.get("params", "{}") or "{}")
    except ValueError:
        params = {}
    job = pbn_lab.Job(settings.MEDIA_ROOT)
    try:
        step = pbn_lab.step_upload(job, f.read(), f.name, params)
    except Exception as exc:
        logger.exception("PBN lab upload")
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"job_id": job.id, "step": step})


@staff_member_required
@csrf_exempt
def pbn_lab_step(request, key):
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    from . import pbn_lab
    job_id = request.POST.get("job_id")
    if not job_id:
        return JsonResponse({"error": "job_id manquant"}, status=400)
    fn = dict(pbn_lab.STEPS).get(key)
    if not fn:
        return JsonResponse({"error": "etape inconnue: %s" % key}, status=400)
    job = pbn_lab.Job(settings.MEDIA_ROOT, job_id=job_id)
    params = (job.load_meta() or {}).get("params", {})
    try:
        step = fn(job, params)
    except Exception as exc:
        logger.exception("PBN lab step %s", key)
        return JsonResponse({"error": str(exc)}, status=500)
    return JsonResponse({"step": step})


# ---------------- Marketing Corner (staff-only) ----------------
@staff_member_required
@xframe_options_sameorigin
def marketing_file(request, uid, name):
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_.-]+$", uid or "") or not _re.match(r"^[A-Za-z0-9_.-]+$", name or ""):
        raise Http404
    path = os.path.join(settings.MEDIA_ROOT, "marketing", uid, name)
    if not os.path.exists(path):
        raise Http404
    if name.endswith(".gif") and request.GET.get("png"):
        # Image fixe : la toile coloriee avec son titre (milieu du 2e plan du GIF)
        from PIL import Image
        import io
        im = Image.open(path)
        n = getattr(im, "n_frames", 1)
        im.seek(min(n - 1, 26))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG", optimize=True)
        resp = HttpResponse(buf.getvalue(), content_type="image/png")
        name = name[:-4] + ".png"
    else:
        ctype = "image/gif" if name.endswith(".gif") else "text/html; charset=utf-8"
        with open(path, "rb") as fh:
            resp = HttpResponse(fh.read(), content_type=ctype)
    if request.GET.get("dl"):
        resp["Content-Disposition"] = 'attachment; filename="%s"' % name
    return resp


def _mkt_form_texts(request, marketing):
    """Textes saisis : {gabarit: {slot: valeur}} + textes du GIF."""
    texts = {}
    for key, _lbl in marketing.TEMPLATES:
        vals = {}
        for sl, _l, _k in marketing.SLOT_LABELS:
            v = request.POST.get("t_%s_%s" % (key, sl))
            if v is not None:
                vals[sl] = v.strip() if sl != "claims" else v.replace("\r", "").strip()
        texts[key] = vals
    gif = {sl: (request.POST.get(sl) or "").strip() for sl, _l, _k in marketing.GIF_SLOTS
           if request.POST.get(sl) is not None}
    return texts, gif


def _next_variant(image_uid):
    from .models import MarketingAd
    used = set(MarketingAd.objects.filter(image_uid=image_uid).values_list("variant", flat=True))
    i = 0
    while True:
        n, name = i, ""
        while True:
            name = chr(65 + n % 26) + name
            n = n // 26 - 1
            if n < 0:
                break
        if name not in used:
            return name
        i += 1


@staff_member_required
def marketing_page(request):
    from . import marketing
    from .models import MarketingAd
    import re as _re, uuid as _uuid, secrets
    from django.contrib import admin as _admin
    ctx = {**_admin.site.each_context(request), "results": None, "error": None, "erp_section": "growth", "gtab": "studio",
           "link": request.POST.get("link", "https://paintit.click/create/"),
           "colors": request.POST.get("colors", "24"),
           "campaign": request.POST.get("campaign", "") or request.GET.get("campaign", ""),
           "variant": request.POST.get("variant", "")}
    from .models import Campaign, PostTemplate
    ctx["campaign_names"] = list(Campaign.objects.values_list("name", flat=True))
    texts, gif = ({}, {})
    base = None
    style = marketing.norm_style(None)
    tpl_id = request.POST.get("tpl") or request.GET.get("tpl") or ""
    ctx["tpl"] = PostTemplate.objects.filter(pk=tpl_id).first() if tpl_id.isdigit() else None
    if request.method == "POST":
        texts, gif = _mkt_form_texts(request, marketing)
        style = marketing.norm_style({k: request.POST.get("s_" + k) for k in ("font", "ink", "accent", "cta", "bg")})
    elif _re.match(r"^mkt-[0-9a-f]{10}$", request.GET.get("img", "")) and os.path.exists(
            os.path.join(settings.MEDIA_ROOT, "orders", request.GET["img"], "%s_preview.svg" % request.GET["img"])):
        ctx["uid"] = request.GET["img"]                # image de la banque deja convertie en toile
        ctx["current_img"] = "/preview/img/%s/source/" % ctx["uid"]
    elif request.GET.get("from"):
        # "Nouvelle variante" depuis la galerie : textes + image d'une variante existante
        ads = list(MarketingAd.objects.filter(image_uid=request.GET["from"],
                                              variant=request.GET.get("v", "")))
        if ads:
            base = ads[0]
            for ad in ads:
                if ad.fmt == "web":
                    texts[ad.kind] = ad.texts
                else:
                    gif = ad.texts
                style = marketing.norm_style((ad.texts or {}).get("_style"))
            ctx.update(uid=base.image_uid, campaign=base.campaign, link=base.target_url)
            ctx["results"] = ads     # apercus de la variante dupliquee
    ctx["promos"] = [{"key": k, "label": lbl, "slots": marketing.template_slots(k, texts.get(k))}
                     for k, lbl in marketing.TEMPLATES]
    ctx["gif_slots"] = marketing.gif_slots(gif)
    ctx["style"], ctx["fonts"] = style, marketing.FONTS
    if base:
        g = next((a for a in ctx["results"] if a.fmt == "gif"), None)
        ctx["current_img"] = "/marketing/file/%s/%s/" % (g.image_uid, g.file_name) if g else ""
    if request.method == "POST":
        f = request.FILES.get("image")
        link = (request.POST.get("link") or "").strip() or settings.SITE_URL + "/create/"
        colors = int(request.POST.get("colors") or 24)
        reuse = (request.POST.get("reuse_uid") or "").strip()
        reuse_ok = bool(reuse) and bool(_re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,39}$", reuse)) and \
            os.path.exists(os.path.join(settings.MEDIA_ROOT, "orders", reuse, "%s_preview.svg" % reuse))
        if not f and not reuse_ok:
            ctx["error"] = "Ajoutez une image."
        else:
            try:
                if reuse_ok and not f:
                    uid = reuse                 # meme image -> nouvelle variante (A/B)
                else:
                    uid = "mkt-" + _uuid.uuid4().hex[:10]
                    tmp = os.path.join(settings.MEDIA_ROOT, "uploads", uid + "_in.jpg")
                    os.makedirs(os.path.dirname(tmp), exist_ok=True)
                    with open(tmp, "wb") as out:
                        for chunk in f.chunks():
                            out.write(chunk)
                    generate(tmp, colors, 40, 50, uid=uid, source_name=f.name)
                variant = _re.sub(r"[^A-Za-z0-9 _-]", "", request.POST.get("variant") or "").strip()[:40] \
                    or _next_variant(uid)
                if MarketingAd.objects.filter(image_uid=uid, variant=variant).exists():
                    variant = variant + "-" + _next_variant(uid)
                suffix = "_" + _re.sub(r"[^A-Za-z0-9]", "", variant).lower()
                keys = [k for k, _l in marketing.TEMPLATES] + ["gif_square", "gif_portrait", "gif_story"]
                tokens = {k: secrets.token_urlsafe(9) for k in keys}
                links = {k: "%s/go/%s/" % (settings.SITE_URL, t) for k, t in tokens.items()}
                produced = marketing.build_all(uid, texts=texts, gif_texts=gif, links=links, suffix=suffix, style=style)
                campaign = (request.POST.get("campaign") or "").strip()[:80]
                for key, label, path, kind in produced:
                    MarketingAd.objects.create(
                        token=tokens[key], image_uid=uid, campaign=campaign, variant=variant, kind=key,
                        label=label, fmt="gif" if kind == "gif" else "web", file_name=os.path.basename(path),
                        target_url=link, texts=dict(gif if kind == "gif" else
                                                    dict(marketing.template_defaults(key), **texts.get(key, {})),
                                                    _style=style))
                ctx.update(uid=uid, variant=variant, campaign=campaign)
                if ctx["tpl"]:      # visuel de la bibliotheque : la nouvelle variante le remplace
                    PostTemplate.objects.filter(pk=ctx["tpl"].pk).update(ad_uid=uid, ad_variant=variant)
                ctx["results"] = list(MarketingAd.objects.filter(image_uid=uid, variant=variant).order_by("id"))
            except Exception as exc:
                logger.exception("Marketing build")
                ctx["error"] = str(exc)
    return render(request, "marketing/index.html", ctx)


# ---------------- Suivi des pubs (public) : clics + vues, attribution des ventes ----------------
_BOT_RX = None


def _visitor_kind(request):
    """'bot' (apercus de liens / robots), 'staff' (tests internes) ou 'human'."""
    global _BOT_RX
    import re as _re
    if _BOT_RX is None:
        _BOT_RX = _re.compile(r"bot\b|bot/|crawl|spider|facebookexternalhit|facebookcatalog|slurp|"
                              r"whatsapp|telegrambot|discordbot|embedly|skypeuripreview|curl/|wget/|"
                              r"python-requests|headlesschrome", _re.I)
    if _BOT_RX.search(request.META.get("HTTP_USER_AGENT", "")):
        return "bot"
    if request.method == "HEAD":
        return "bot"
    user = getattr(request, "user", None)
    if user and user.is_authenticated and user.is_staff:
        return "staff"
    return "human"


def _first_time(request, ad, what):
    """True la 1re fois pour ce visiteur (session) et cette pub."""
    k = "ad_%s_%s" % (what, ad.pk)
    if request.session.get(k):
        return False
    request.session[k] = 1
    return True


def ad_click(request, token):
    from django.db.models import F
    from django.utils import timezone
    from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
    from .models import MarketingAd
    ad = MarketingAd.objects.filter(token=token).first()
    if not ad:
        return redirect("studio:home")
    who = _visitor_kind(request)
    if who == "human":
        upd = {"clicks": F("clicks") + 1, "last_click_at": timezone.now()}
        if _first_time(request, ad, "c"):
            upd["unique_clicks"] = F("unique_clicks") + 1
        MarketingAd.objects.filter(pk=ad.pk).update(**upd)
    elif who == "staff":   # tests internes : visibles mais hors statistiques A/B
        MarketingAd.objects.filter(pk=ad.pk).update(test_clicks=F("test_clicks") + 1,
                                                     last_click_at=timezone.now())
    if who != "bot":
        request.session["ad_ref"] = ad.token          # attribution de la vente (dernier clic)
    u = urlparse(ad.target_url or settings.SITE_URL + "/create/")
    q = dict(parse_qsl(u.query))
    q.update({"utm_source": "paintit_ad", "utm_medium": ad.fmt, "utm_campaign": ad.campaign or ad.image_uid,
              "utm_content": "%s-%s" % (ad.variant, ad.kind)})
    resp = redirect(urlunparse(u._replace(query=urlencode(q))))
    resp["Cache-Control"] = "no-store"   # jamais de redirection mise en cache (sinon clics perdus)
    return resp


@xframe_options_exempt   # une pub peut etre integree (iframe) sur un site partenaire
def ad_view(request, token):
    from django.db.models import F
    from .models import MarketingAd
    ad = MarketingAd.objects.filter(token=token).first()
    path = ad and os.path.join(settings.MEDIA_ROOT, "marketing", ad.image_uid, ad.file_name)
    if not ad or not os.path.exists(path):
        raise Http404
    if _visitor_kind(request) == "human" and _first_time(request, ad, "v"):
        MarketingAd.objects.filter(pk=ad.pk).update(views=F("views") + 1)
    ctype = "image/gif" if ad.fmt == "gif" else "text/html; charset=utf-8"
    with open(path, "rb") as fh:
        resp = HttpResponse(fh.read(), content_type=ctype)
    resp["Cache-Control"] = "no-store"   # chaque affichage passe par le compteur
    return resp


@staff_member_required
def marketing_gallery(request):
    """Galerie A/B : pubs groupees par image, variantes comparees par gabarit."""
    from collections import OrderedDict
    from django.db.models import Count, Sum
    from django.contrib import admin as _admin
    from .models import MarketingAd, Order
    if request.method == "POST":
        ad = MarketingAd.objects.filter(pk=request.POST.get("ad")).first()
        op = request.POST.get("op")
        if ad and op == "toggle":
            ad.active = not ad.active; ad.save(update_fields=["active"])
        elif ad and op == "reset":
            MarketingAd.objects.filter(pk=ad.pk).update(views=0, clicks=0)
        return redirect(request.get_full_path())
    show = request.GET.get("show", "active")
    qs = MarketingAd.objects.all()
    if show == "active":
        qs = qs.filter(active=True)
    conv = {r["ad_ref"]: r for r in Order.objects.filter(status__in=Order.PAID_STATUSES).exclude(ad_ref="")
            .values("ad_ref").annotate(n=Count("id"), ca=Sum("total"))}
    groups = OrderedDict()
    for ad in qs.order_by("-created_at", "id"):
        c = conv.get(ad.token, {})
        ad.orders, ad.revenue = c.get("n", 0), round(c.get("ca") or 0, 2)
        ad.conv = round(ad.orders / ad.clicks * 100, 1) if ad.clicks else None
        ad.public_url = "%s/m/%s/" % (settings.SITE_URL, ad.token)
        ad.track_url = "%s/go/%s/" % (settings.SITE_URL, ad.token)
        ad.headline = ad.texts.get("headline") or ad.texts.get("gif_title") or ""
        ad.cta = ad.texts.get("cta") or ad.texts.get("gif_cta") or ""
        g = groups.setdefault(ad.image_uid, {"uid": ad.image_uid, "campaign": ad.campaign, "created": ad.created_at,
                                              "ads": [], "variants": OrderedDict(), "views": 0, "clicks": 0,
                                              "orders": 0, "revenue": 0.0})
        g["ads"].append(ad)
        g["variants"].setdefault(ad.variant, []).append(ad)
        g["views"] += ad.views; g["clicks"] += ad.clicks; g["orders"] += ad.orders; g["revenue"] += ad.revenue
        g["tests"] = g.get("tests", 0) + ad.test_clicks
        g["campaign"] = g["campaign"] or ad.campaign
    # Gagnant par gabarit (au moins 2 variantes, meilleur taux de clic avec >= 20 vues, sinon plus de clics)
    for g in groups.values():
        by_kind = OrderedDict()
        for ad in g["ads"]:
            by_kind.setdefault(ad.kind, []).append(ad)
        g["kinds"] = []
        for kind, ads in by_kind.items():
            ads.sort(key=lambda a: a.variant)
            if len(ads) >= 2:
                ranked = sorted(ads, key=lambda a: ((a.ctr or 0) if a.views >= 20 else -1, a.unique_clicks, a.orders),
                                reverse=True)
                if ranked[0].clicks > 0:
                    ranked[0].winner = True
            g["kinds"].append({"kind": kind, "label": ads[0].label, "ads": ads})
        order = ["slider", "shiny", "zoom", "gif_square", "gif_portrait", "gif_story"]
        g["kinds"].sort(key=lambda k: order.index(k["kind"]) if k["kind"] in order else 99)
        g["uclicks"] = sum(a.unique_clicks for a in g["ads"])
        g["ctr"] = round(min(g["uclicks"], g["views"]) / g["views"] * 100, 1) if g["views"] else None
        g["last_variant"] = list(g["variants"])[-1]
    tot = MarketingAd.objects.aggregate(v=Sum("views"), c=Sum("clicks"), u=Sum("unique_clicks"),
                                        t=Sum("test_clicks"), n=Count("id"))
    att = Order.objects.filter(status__in=Order.PAID_STATUSES).exclude(ad_ref="").aggregate(n=Count("id"), ca=Sum("total"))
    return render(request, "admin/erp_marketing.html", {
        **_admin.site.each_context(request), "groups": list(groups.values()), "show": show,
        "tot": {"ads": tot["n"] or 0, "views": tot["v"] or 0, "clicks": tot["c"] or 0,
                "unique": tot["u"] or 0, "tests": tot["t"] or 0,
                "ctr": round(min(tot["u"] or 0, tot["v"]) / tot["v"] * 100, 1) if tot["v"] else None,
                "orders": att["n"] or 0, "revenue": round(att["ca"] or 0, 2)},
        "erp_section": "growth", "gtab": "ads"})


# ---------------- Hub ERP (tableau de bord admin) ----------------
def _csv_cell(v):
    """Neutralise l'injection de formules (Excel/LibreOffice) dans les exports CSV."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def _csv_response(name, header, rows):
    import csv
    from django.http import HttpResponse
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="%s"' % name
    resp.write("\ufeff")   # BOM : accents corrects a l'ouverture dans Excel
    w = csv.writer(resp, delimiter=";")
    w.writerow(header)
    for r in rows:
        w.writerow([_csv_cell(c) for c in r])
    return resp


@staff_member_required
def orders_csv(request):
    from .models import Order
    rows = ([o.uid, o.created_at.strftime("%Y-%m-%d %H:%M"), o.get_status_display(), o.format_label,
             o.colors, "oui" if o.brushes else "non", o.price, o.discount_code or "", o.discount_amount,
             o.total, o.cost, o.benefit, o.customer_name, o.customer_email, o.city, o.country,
             o.supplier_ref or "", o.carrier, o.tracking_number]
            for o in Order.objects.order_by("-created_at"))
    return _csv_response("commandes_paintit.csv",
                         ["uid", "date", "statut", "format", "couleurs", "pinceaux", "prix", "code_remise",
                          "remise_eur", "total_eur", "cout_eur", "marge_eur", "client", "email", "ville",
                          "pays", "fournisseur_ref", "transporteur", "suivi"], rows)


def _test_scope(view):
    """Pilotage / compta : chiffres sur les donnees reelles ; ?test=1 inclut les commandes de test."""
    import functools

    @functools.wraps(view)
    def wrap(request, *a, **kw):
        from .models import SHOW_TEST
        tok = SHOW_TEST.set(request.GET.get("test") == "1")
        try:
            return view(request, *a, **kw)
        finally:
            SHOW_TEST.reset(tok)
    return wrap


def _test_info(request):
    from .models import LedgerEntry, stripe_test_mode
    return {"show": request.GET.get("test") == "1", "live": not stripe_test_mode(),
            "orders": Order.objects.filter(is_test=True).count(),
            "entries": LedgerEntry.objects.filter(is_test=True).count(),
            "real_orders": Order.objects.filter(is_test=False).count()}


@staff_member_required
def erp_test_data(request):
    """Donnees de test : tout marquer comme test (repartir des seules vraies commandes), purger, ou annuler."""
    from django.contrib import messages
    from .models import LedgerEntry
    back = request.POST.get("next") or "/admin-hub/"
    if not back.startswith("/admin-hub/"):
        back = "/admin-hub/"
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "mark_all":
            n = Order.objects.filter(is_test=False).update(is_test=True)
            e = LedgerEntry.objects.filter(is_test=False).update(is_test=True)
            messages.success(request, "Remise à zéro : %d commande(s) et %d écriture(s) passées en test. "
                                      "Pilotage et comptabilité repartent des prochaines vraies commandes." % (n, e))
        elif op == "purge":
            if (request.POST.get("confirm") or "").strip().upper() != "SUPPRIMER":
                messages.error(request, "Tapez SUPPRIMER pour confirmer la suppression définitive.")
            else:
                e = LedgerEntry.objects.filter(is_test=True).delete()[0]
                n = _delete_orders(Order.objects.filter(is_test=True))
                messages.success(request, "%d commande(s) de test et %d écriture(s) supprimées définitivement." % (n, e))
        elif op == "toggle":
            o = Order.objects.filter(pk=request.POST.get("id")).first()
            if o:
                o.is_test = not o.is_test
                o.save(update_fields=["is_test"])
                LedgerEntry.objects.filter(order=o).update(is_test=o.is_test)
                OrderEvent.objects.create(order=o, kind="action", user=request.user.get_username(),
                                          text="Marquée comme %s" % ("commande de test" if o.is_test else "vraie commande"))
    return redirect(back)


@staff_member_required
@_test_scope
def admin_hub(request):
    from . import erp
    from .models import Order, ContactMessage
    period = request.GET.get("p", "30d")
    k = erp.kpis(period)
    ctx = {"k": k, "series": erp.series(k["period"]), "mix": erp.mix(k["period"]),
           "pipeline": erp.pipeline(), "actions": erp.actions(), "cust": erp.customer_stats(),
           "recent_orders": list(Order.objects.order_by("-created_at")[:8]),
           "recent_msgs": list(ContactMessage.objects.order_by("answered", "-created_at")[:6]),
           "gallery_all": erp.gallery_sales(), "erp_section": "hub", "testdata": _test_info(request)}
    from django.contrib import admin as _admin
    ctx.update(_admin.site.each_context(request))
    return render(request, "admin/hub.html", ctx)


@staff_member_required
def erp_customers(request):
    from . import erp
    if request.method == "POST" and request.POST.get("op") == "delete":
        from django.contrib import messages
        email = (request.POST.get("email") or "").strip()
        n = _delete_customer(email)
        messages.success(request, "Client %s supprimé (%d commande%s, toiles et messages)." % (email, n, "s" if n > 1 else ""))
        return redirect(request.get_full_path())
    q = (request.GET.get("q") or "").strip()
    sort = request.GET.get("sort") or "-revenue"
    seg = request.GET.get("seg") or ""
    rows = erp.customers(q=q, sort=sort, segment=seg)
    if request.GET.get("format") == "csv":
        return _csv_response("clients_paintit.csv",
                             ["email", "nom", "segment", "commandes_payees", "tentatives", "ca_eur", "marge_eur",
                              "premiere", "derniere", "ville", "pays", "toiles_numeriques"],
                             ([r["customer_email"], r["name"], r["segment"], r["orders"], r["attempts"],
                               r["revenue"], r["margin"], r["first"].strftime("%Y-%m-%d"),
                               r["last"].strftime("%Y-%m-%d"), r["city"], r["country"], r["canvases"]]
                              for r in rows))
    from django.contrib import admin as _admin
    return render(request, "admin/erp_customers.html", {**_admin.site.each_context(request),
        "rows": rows[:500], "count": len(rows), "q": q, "sort": sort, "seg": seg,
        "stats": erp.customer_stats(), "erp_section": "customers",
        "segments": [("", "Tous"), ("vip", "VIP"), ("fidele", "Fideles"), ("client", "Clients"),
                     ("inactif", "Inactifs"), ("prospect", "Prospects")]})


def _grant_gallery(request, m, email):
    """Ajoute le modele achete aux digipaints de l'utilisateur. -> True si nouvellement attribue."""
    created = _grant_gallery_to(m, email)
    if request is not None:
        uids = request.session.get("my_uids", [])
        if m.uid not in uids:
            uids.append(m.uid); request.session["my_uids"] = uids[-60:]
    return created


def _grant_gallery_to(m, email):
    return DigitalCanvas.objects.get_or_create(
        email=email, uid=m.uid,
        defaults=dict(title=m.title, category=m.category, colors=m.colors,
                      orientation=m.orientation, width_cm=m.width_cm, height_cm=m.height_cm,
                      source="library", price=0))[1]


def gallery_buy(request, uid):
    m = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
    if not m:
        raise Http404
    if m.hidden:
        return redirect("studio:gallery")
    price = float(m.price or 0)
    if price <= 0:
        return redirect("studio:digipaint", uid=uid)
    verified = request.session.get("verified_email") or None      # sinon Stripe demande l'e-mail
    if (verified and DigitalCanvas.objects.filter(email=verified, uid=uid).exists()) \
            or uid in request.session.get("dl_uids", []):
        return redirect("studio:digipaint", uid=uid)   # deja possede
    if payments.stripe_live():
        try:
            return redirect(payments.create_gallery_session(uid, request, price, verified, title=m.title))
        except Exception:
            logger.exception("Stripe galerie %s", uid)
            return JsonResponse({"error": "paiement indisponible"}, status=502)
    if not payments.demo_allowed():
        return JsonResponse({"error": "paiement indisponible"}, status=503)
    if verified:                                       # demo (dev) : credit immediat
        _grant_gallery(request, m, verified)
    _grant_download(request, uid)
    return redirect("studio:digipaint", uid=uid)


def gallery_buy_success(request):
    uid = request.GET.get("uid", ""); sid = request.GET.get("sid", "")
    sess = payments.paid_session(sid, uid=uid, kind="gallery") if uid else None
    if sess:
        m = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
        email = _buyer_email(request, sess)
        if m and email:
            if _grant_gallery(request, m, email):
                _digital_receipt(email, uid, m.title, sess)
        if m:
            _grant_download(request, uid)
            return redirect(reverse("studio:digipaint", args=[uid]) + "?bought=1")
    return redirect("studio:digipaint", uid=uid)


# ---------------- Fournisseurs (tableau de bord facon Shopify) ----------------
def _sup_color(name):
    import hashlib
    cols = ["#2f6bf2", "#7a3ff2", "#1e8848", "#e8703b", "#e85d75", "#0f9bb3", "#b3287a", "#9a6300"]
    return cols[int(hashlib.md5((name or "?").encode()).hexdigest(), 16) % len(cols)]


SUPPLIER_CHANNELS = [
    ("woo", "MangoERP", "Son ERP récupère nos commandes"),
    ("api", "API", "Envoi vers son serveur"),
    ("email", "E-mail", "Commande et fichiers par e-mail"),
]

def _grid_view(sup):
    """Grille tarifaire du fournisseur, prete a afficher/editer (groupee par produit, comme le devis)."""
    from .models import KitFormat, format_key
    g = sup.grid()
    cols = [str(c) for c in g.get("cols") or []]
    zones = g.get("zones") or []
    sold = {k.key for k in KitFormat.objects.all()}
    rows = list(g.get("rows") or [])
    prods = []
    for j, p in enumerate(g.get("products") or []):
        pr = [dict(r, i=i, fmt=format_key(*r["size"].split("x")) if "x" in r.get("size", "") else r.get("size", ""),
                   prices=[(c, (r.get("price") or {}).get(c)) for c in cols],
                   ships=[(z["key"], (r.get("ship") or {}).get(z["key"])) for z in zones])
              for i, r in enumerate(rows) if r.get("product") == p.get("key")]
        for r in pr:
            r["sold"] = r["fmt"] in sold
        prods.append(dict(p, j=j, rows=pr, extras=[(c, (p.get("extra") or {}).get(c)) for c in cols]))
    missing = [z["label"] for z in zones if not any((r.get("ship") or {}).get(z["key"]) not in (None, "")
                                                    for r in rows if r.get("on"))]
    return {"g": g, "cols": cols, "zones": zones, "products": prods, "n_rows": len(rows), "missing_zones": missing,
            "all_cols": [str(n) for n in (12, 24, 36, 48)],
            "currencies": [("USD", "Dollar US ($)"), ("EUR", "Euro (€)"), ("CNY", "Yuan (¥)"), ("GBP", "Livre (£)")],
            "sym": {"USD": "$", "EUR": "€", "CNY": "¥", "GBP": "£"}.get(g.get("currency") or "EUR", "")}


def _grid_save(sup, P):
    """Enregistre la grille postee depuis la fiche (structure : voir Supplier.GRID_TEMPLATE)."""
    import re as _re
    from .models import format_key

    def num(v):
        v = (v or "").replace(",", ".").replace("$", "").replace("€", "").strip()
        try:
            return round(float(v), 4) if v else None
        except ValueError:
            return None
    old = sup.grid()
    cols = sorted({int(c) for c in P.getlist("g_cols") if c.isdigit()}) or [24]
    zones = []
    for i in range(int(P.get("g_nz") or 0) + 1):
        lbl = (P.get("z_label_%d" % i) or "").strip()[:40]
        if not lbl or P.get("z_del_%d" % i):
            continue
        key = (P.get("z_key_%d" % i) or "").strip() or ("z%d" % i)
        while any(z["key"] == key for z in zones):
            key += "_"
        zones.append({"key": key, "label": lbl, "countries": (P.get("z_countries_%d" % i) or "").upper().strip()[:200],
                      "days": (P.get("z_days_%d" % i) or "").strip()[:30]})
    prods = []
    for j in range(int(P.get("g_np") or 0) + 1):
        lbl = (P.get("p_label_%d" % j) or "").strip()[:60]
        if not lbl or P.get("p_del_%d" % j):
            continue
        key = (P.get("p_key_%d" % j) or "").strip() or _re.sub(r"[^a-z0-9]+", "_", lbl.lower()).strip("_") or "p%d" % j
        prods.append({"key": key, "label": lbl, "kit": P.get("g_kit") == key,
                      "extra": {str(c): num(P.get("p_extra_%d_%d" % (j, c))) for c in cols}})
    if prods and not any(p["kit"] for p in prods):
        prods[0]["kit"] = True
    keys = {p["key"] for p in prods}
    rows = []
    for i in range(int(P.get("g_nr") or 0) + 3):
        size = (P.get("r_size_%d" % i) or "").lower().replace(" ", "").replace("*", "x").replace("×", "x")
        prod = P.get("r_prod_%d" % i)
        if not _re.fullmatch(r"\d{2,3}x\d{2,3}", size) or prod not in keys or P.get("r_del_%d" % i):
            continue
        row = {"product": prod, "size": format_key(*size.split("x")), "on": bool(P.get("r_on_%d" % i)),
               "price": {str(c): num(P.get("r_price_%d_%d" % (i, c))) for c in cols},
               "ship": {z["key"]: num(P.get("r_ship_%d_%s" % (i, z["key"]))) for z in zones}}
        if any(r["product"] == prod and r["size"] == row["size"] for r in rows):
            continue        # taille deja presente pour ce produit
        if i >= int(P.get("g_nr") or 0) and not any(v is not None for v in row["price"].values()):
            continue        # ligne d'ajout laissee vide
        rows.append(row)
    order = {p["key"]: n for n, p in enumerate(prods)}
    rows.sort(key=lambda r: (order.get(r["product"], 99), [int(x) for x in r["size"].split("x")]))
    cur = P.get("g_currency") if P.get("g_currency") in ("USD", "EUR", "CNY", "GBP") else "EUR"
    offer = dict(sup.offer or {})
    offer["grid"] = {"currency": cur, "fx": 1.0 if cur == "EUR" else (num(P.get("g_fx")) or old.get("fx") or 1.0),
                     "cols": cols, "zones": zones, "products": prods, "rows": rows,
                     "note": (P.get("g_note") or "").strip()[:500]}
    offer["extras"] = {"brushes": {"on": bool(P.get("brushes_on")), "cost": num(P.get("brushes_cost"))}}
    offer["shipping"] = {**(offer.get("shipping") or {}), "handling": num(P.get("ship_handling"))}
    offer["taxes"] = {"vat_rate": num(P.get("tax_vat_rate")) or 0.0, "vat_recoverable": bool(P.get("tax_vat_recoverable")),
                      "duties_rate": num(P.get("tax_duties_rate")) or 0.0, "customs_fee": num(P.get("tax_customs_fee")) or 0.0}
    sup.offer = offer
    sup.save(update_fields=["offer"])
    return len(rows)


def _sup_card(sup):
    from . import erp
    sup.stats = erp.supplier_stats(sup)
    sup.color = _sup_color(sup.name)
    sup.initials = "".join(w[0] for w in (sup.name or "?").split()[:2]).upper()
    sup.channel = ("MangoERP" if sup.integration == "woo" and (sup.woo or {}).get("ck") else
                   "API" if sup.integration == "api" and sup.api_url else
                   "E-mail" if sup.email else None)
    sup.health = ("off" if not sup.active else "err" if not sup.channel or not sup.last_sync_ok
                  else "ok" if sup.last_sync_at else "idle")
    return sup


@staff_member_required
def erp_suppliers(request):
    from django.contrib import admin as _admin
    from . import erp
    from .models import Supplier
    if request.method == "POST":
        sup = Supplier.objects.filter(pk=request.POST.get("sup")).first()
        if sup and request.POST.get("op") == "toggle" and not (sup.woo or {}).get("test"):
            sup.active = not sup.active; sup.save(update_fields=["active"])
        if request.POST.get("op") == "woo_test":
            from django.contrib import messages
            from . import woo_test
            shop = woo_test.create_shop()
            if not woo_test.test_orders(shop).exists():
                try:
                    woo_test.add_order(shop, user=request.user.get_username())
                except ValueError as exc:
                    messages.error(request, str(exc))
            return redirect(reverse("studio:erp_supplier", args=[shop.pk]) + "#integration")
        return redirect("studio:erp_suppliers")
    sups = [_sup_card(s) for s in Supplier.objects.all()]
    routing = {c: Supplier.for_checkout(c) for c in ("kit", "print")}
    for s in sups:
        s.routed = any(r and r.pk == s.pk for r in routing.values())
    tot = {"active": sum(1 for s in sups if s.active),
           "orders_30": sum(s.stats["orders_30"] for s in sups),
           "spend_30": round(sum(s.stats["spend_30"] for s in sups), 2),
           "in_production": sum(s.stats["in_production"] for s in sups),
           "late": sum(s.stats["late"] for s in sups),
           "errors": sum(1 for s in sups if s.health == "err")}
    return render(request, "admin/erp_suppliers.html", {
        **_admin.site.each_context(request), "sups": sups, "tot": tot, "routing": routing,
        "unassigned": erp.unassigned_orders().count(), "erp_section": "suppliers"})


def _woo_test_ctx(sup):
    """Boutique test : commandes TEST + carte « information -> champ WooCommerce -> ou dans MangoERP »."""
    if not (sup.woo or {}).get("test"):
        return None
    from . import woo_test
    orders = list(woo_test.test_orders(sup)[:10])
    notes = (sup.woo or {}).get("map") or {}
    rows = [{"key": k, "info": i, "field": f, "value": v, "where": notes.get(k, "")}
            for k, i, f, v in (woo_test.field_map(orders[0]) if orders else [])]
    return {"orders": orders, "rows": rows}


@staff_member_required
def erp_supplier(request, pk):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from .models import Supplier
    from . import erp
    sup = Supplier.objects.filter(pk=pk).first()
    if not sup:
        raise Http404
    if request.method == "POST":
        op = request.POST.get("op")
        user = request.user.get_username()
        tab = (request.POST.get("_tab") or "").strip()[:20]
        if op == "save":
            sec = request.POST.get("section", "")
            if sec in SUPPLIER_SECTIONS:
                form = _supplier_section_form(sec)(request.POST, instance=sup)
                if form.is_valid():
                    sup = form.save()
                    notes = _save_supplier_extras(request, sup, SUPPLIER_SECTION_EXTRAS.get(sec, ()))
                    messages.success(request, "Modifications enregistrées." + ("" if not notes else " " + " ".join(notes)))
                else:
                    messages.error(request, "Non enregistré : " + " ; ".join(
                        "%s : %s" % (form.fields[k].label if k in form.fields else k, " ".join(v))
                        for k, v in form.errors.items()))
            return redirect(request.path + ("#" + tab if tab else ""))
        if op == "delete":
            name, spk = sup.name, sup.pk
            sup.delete()
            import shutil   # pieces jointes + modele Excel du fournisseur
            shutil.rmtree(os.path.join(settings.MEDIA_ROOT, "private", "suppliers", str(spk)), ignore_errors=True)
            messages.success(request, "Fournisseur %s supprimé." % name)
            return redirect("studio:erp_suppliers")
        if op == "toggle" and not (sup.woo or {}).get("test"):     # la boutique test ne doit jamais recevoir de vraie commande
            sup.active = not sup.active; sup.save(update_fields=["active"])
        elif op == "resend":
            o = Order.objects.filter(pk=request.POST.get("order")).first()
            if o:
                ok = _notify_supplier(_order_from_row(o), _shipping_from_row(o), sup=sup, user=user)
                messages.success(request, "Commande %s transmise." % o.uid) if ok else \
                    messages.error(request, "Echec de transmission de %s (voir journal)." % o.uid)
        elif op == "test":
            from . import integrations
            ok, channel, err = integrations.test_connection(sup, user=user)
            (messages.success if ok else messages.error)(
                request, ("Test réussi (%s) : le fournisseur a bien reçu l'événement « test »." % channel) if ok
                else "Test en échec (%s) : %s" % (channel or "aucun canal", err))
        elif op == "secret":
            import secrets
            sup.webhook_secret = secrets.token_urlsafe(24); sup.save(update_fields=["webhook_secret"])
            messages.success(request, "Nouveau secret généré : transmettez la nouvelle URL de retour au fournisseur.")
        elif op == "auto":
            sup.auto_dispatch = not sup.auto_dispatch; sup.save(update_fields=["auto_dispatch"])
            messages.success(request, "Transmission %s." % ("automatique au paiement" if sup.auto_dispatch
                                                            else "manuelle (validation depuis la fiche commande)"))
        elif op == "palette_upload":
            from . import palette as _pal
            f = request.FILES.get("csv")
            if not f:
                messages.error(request, "Choisissez un fichier CSV.")
            else:
                cols, rep = _pal.parse_csv(f.read())
                if not cols:
                    messages.error(request, "Aucune couleur lue : 1re colonne = référence, 2e = code hexadécimal (#RRGGBB).")
                else:
                    sup.palette = cols; sup.palette_updated_at = timezone.now()
                    sup.save(update_fields=["palette", "palette_updated_at"])
                    note = []
                    if rep["duplicates"]:
                        note.append("%d doublon(s) ignoré(s) (réf. %s)" % (len(rep["duplicates"]), ", ".join(rep["duplicates"][:8])))
                    if rep["same_code"]:
                        note.append("même référence pour 2 teintes : %s" % ", ".join(rep["same_code"][:8]))
                    if rep["invalid"]:
                        note.append("%d ligne(s) illisible(s)" % len(rep["invalid"]))
                    messages.success(request, "Nuancier importé : %d couleurs disponibles.%s Les prochaines toiles "
                                     "utiliseront uniquement ces teintes." % (len(cols), (" " + " ; ".join(note) + ".") if note else ""))
        elif op == "palette_clear":
            sup.palette = []; sup.palette_updated_at = None
            sup.save(update_fields=["palette", "palette_updated_at"])
            messages.success(request, "Nuancier retiré : les couleurs seront de nouveau calculées librement.")
        elif op in ("retry_now", "retry_skip"):
            from . import integrations
            ids = set(request.POST.getlist("oid"))
            todo = [o for o in integrations.pending_retries() if o.supplier_id == sup.pk and (not ids or str(o.pk) in ids)]
            n = 0
            for o in todo:
                if op == "retry_now":
                    if _notify_supplier(_order_from_row(o), _shipping_from_row(o), sup=sup, user=user):
                        n += 1
                        Order.objects.filter(pk=o.pk, status=Order.PAID).update(status=Order.FULFILLED)
                else:
                    OrderEvent.objects.create(order=o, kind="action", user=user,
                                              text="%s : ne pas transmettre à %s" % (integrations.SKIP_PREFIX, sup.name))
                    n += 1
            messages.success(request, ("%d/%d commande(s) transmise(s) via %s." % (n, len(todo), sup.get_integration_display()))
                             if op == "retry_now" else "%d commande(s) retirée(s) des relances (non transmises)." % n)
        elif op == "grid_save":
            n = _grid_save(sup, request.POST)
            messages.success(request, "Grille enregistrée (%d ligne%s)." % (n, "s" if n > 1 else ""))
            return redirect(reverse("studio:erp_supplier", args=[sup.pk]) + "#tarifs")
        elif op == "grid_seed":
            if not sup.grid():
                sup.offer = {**(sup.offer or {}), "grid": Supplier.GRID_TEMPLATE}
                sup.save(update_fields=["offer"])
                messages.success(request, "Devis Yingsa repris. Vérifiez le taux USD → € et complétez la livraison France / UE.")
            return redirect(reverse("studio:erp_supplier", args=[sup.pk]) + "#tarifs")
        elif op == "channel":
            ch = request.POST.get("integration", "")
            if ch in dict(Supplier.INTEGRATIONS) and ch != sup.integration:
                sup.integration = ch
                sup.save(update_fields=["integration"])
                if ch == "woo" and not (sup.woo or {}).get("ck"):
                    from . import woo_api
                    woo_api.generate_keys(sup)
                messages.success(request, "Canal : %s." % sup.get_integration_display())
        elif op in ("woo_test_order", "woo_test_delete", "woo_test_notes") and (sup.woo or {}).get("test"):
            from . import woo_test
            if op == "woo_test_delete":
                woo_test.delete_shop(sup)
                messages.success(request, "Boutique test supprimée (commandes TEST et fichiers compris).")
                return redirect("studio:erp_suppliers")
            if op == "woo_test_notes":      # « où le trouver dans MangoERP » : brouillon du tuto
                sup.woo = {**sup.woo, "map": {k[4:]: v.strip()[:200] for k, v in request.POST.items()
                                              if k.startswith("map_") and v.strip()}}
                sup.save(update_fields=["woo"])
                messages.success(request, "Emplacements enregistrés.")
            else:
                try:
                    o = woo_test.add_order(sup, request.POST.get("src", "").strip(), user=user)
                    messages.success(request, "Commande %s créée : dans MangoERP, 订单 → 同步订单." % o.uid)
                except ValueError as exc:
                    messages.error(request, str(exc))
        elif op in ("woo_keys", "woo_revoke"):
            from . import woo_api
            if op == "woo_keys":
                woo_api.generate_keys(sup)
                messages.success(request, "Nouveaux accès générés. Les précédents ne fonctionnent plus.")
            else:
                woo_api.revoke_keys(sup)
                messages.success(request, "Accès révoqués.")
        elif op == "tracking_import":
            from . import xlsx_bridge
            f = request.FILES.get("tracking")
            if not f:
                messages.error(request, "Choisissez le fichier de suivi (.xlsx ou .csv) exporté par le fournisseur.")
            else:
                n, errs = xlsx_bridge.import_tracking(sup, f.name, f.read())
                (messages.success if n else messages.error)(
                    request, "%d commande(s) passée(s) en « Expédiée » avec leur n° de suivi.%s" % (
                        n, (" Ignorées : " + " ; ".join(errs[:6])) if errs else ""))
        elif op == "pay_mark":
            from . import ledger
            ids = [int(x) for x in request.POST.getlist("oid") if x.isdigit()]
            pay = ledger.prepare_payment(sup, list(Order.objects.filter(supplier=sup, pk__in=ids)), user=user)
            if pay:
                return redirect("studio:erp_supplier_payment", pk=pay.pk)
            messages.error(request, "Aucune commande à régler dans la sélection.")
        elif op == "assign":
            n = 0
            for o in erp.unassigned_orders():
                Order.objects.filter(pk=o.pk).update(supplier=sup); n += 1
            messages.success(request, "%d commande(s) rattachée(s) à %s." % (n, sup.name))
        return redirect(request.get_full_path() + ("#" + tab if tab else ""))
    _sup_card(sup)
    tab = request.GET.get("o", "open")
    qs = Order.objects.filter(supplier=sup).order_by("-created_at")
    tabs = [("open", "À traiter", qs.filter(status__in=(Order.PAID,) + Order.AT_SUPPLIER)),
            ("shipped", "Expédiées", qs.filter(status=Order.SHIPPED)),
            ("delivered", "Livrées", qs.filter(status=Order.DELIVERED)),
            ("failed", "Échecs", qs.filter(status=Order.FAILED)),
            ("all", "Toutes", qs)]
    current = dict((k, q) for k, _l, q in tabs).get(tab, tabs[0][2])
    now = timezone.now()
    p = Pricing.get()
    rows = []
    for o in current[:100]:
        age = (now - (o.status_changed_at or o.created_at)).days
        late = (o.status in Order.AT_SUPPLIER and age >= sup.stats["lead"]) or \
               (o.status == Order.SHIPPED and age >= int(p.delivery_days_max or 9) + 3)
        rows.append({"o": o, "age": age, "late": late})
    events = (OrderEvent.objects.filter(order__supplier=sup, kind="action").select_related("order")[:12])
    def mask(v, keep=4):
        v = v or ""
        return ("•••• " + v[-keep:]) if len(v) > keep else v
    import json as _json
    from . import integrations
    last = Order.objects.filter(supplier=sup).order_by("-created_at").first() or Order.objects.order_by("-created_at").first()
    sample_out = integrations.build_payload(sup, _order_from_row(last), _shipping_from_row(last)) if last else \
        integrations.build_payload(sup, {"uid": "0844E1DF-6FED", "format_label": "40 x 50 cm", "width_cm": 40,
                                         "height_cm": 50, "colors": 24, "total": 42.9}, {})
    sample_in = {"uid": sample_out["order"]["uid"], "status": "shipped", "carrier": "Colissimo",
                 "tracking_number": "6A12345678901", "tracking_url": "https://www.laposte.fr/outils/suivre-vos-envois?code=6A12345678901",
                 "supplier_ref": "SUP-000123", "message": "Colis remis au transporteur"}
    from datetime import timedelta
    from django.db import models
    from .models import ConnectorLog
    retries = [x for x in integrations.pending_retries() if x.supplier_id == sup.pk]
    automation = {"callback": integrations.callback_url(sup), "retries": len(retries), "retry_rows": retries,
                  "out": _json.dumps(sample_out, ensure_ascii=False, indent=2),
                  "in": _json.dumps(sample_in, ensure_ascii=False, indent=2)}
    due = list(Order.objects.filter(supplier=sup, status__in=(Order.FULFILLED, Order.PRODUCTION, Order.SHIPPED, Order.DELIVERED),
                                    supplier_paid_at__isnull=True, supplier_payment__isnull=True).order_by("created_at"))
    paid = list(sup.payments.all()[:10])
    return render(request, "admin/erp_supplier.html", {
        "automation": automation, "due": due, "due_total": round(sum(o.cost or 0 for o in due), 2), "paid": paid,
        "xl": (sup.offer or {}).get("xlsx") or {},
        "channels": SUPPLIER_CHANNELS, "grid": _grid_view(sup), "offer_x": sup.offer or {},
        "forms": {k: _supplier_section_form(k)(instance=sup) for k in SUPPLIER_SECTIONS},
        "xcols": [{"i": i, "header": h, "field": (((sup.offer or {}).get("xlsx") or {}).get("map") or {}).get(str(i), "")}
                  for i, h in enumerate(((sup.offer or {}).get("xlsx") or {}).get("headers") or [])],
        "xfields": __import__("studio.xlsx_bridge", fromlist=["x"]).FIELDS,
        "xsku_default": __import__("studio.xlsx_bridge", fromlist=["x"]).DEFAULT_SKU,
        "attention": _supplier_attention(sup),
        "woo": sup.woo or {}, "woo_site": settings.SITE_URL, "woo_test": _woo_test_ctx(sup),
        "woo_logs": (ConnectorLog.objects.filter(models.Q(supplier=sup) | models.Q(
                         supplier__isnull=True, at__gte=timezone.now() - timedelta(days=7)))[:20]
                     if sup.integration == "woo" else []),
        **_admin.site.each_context(request), "sup": sup, "tab": tab, "rows": rows,
        "tabs": [(k, l, q.count()) for k, l, q in tabs], "events": events,
        "iban": mask(sup.iban), "api_key": mask(sup.api_key, 3),
        "files": [lbl for flag, lbl in [("want_template_pdf", "Toile numérotée PDF vectoriel"),
                                        ("want_template_svg", "Toile numérotée .svg"),
                                        ("want_template_tiff", "Toile numérotée .tiff"),
                                        ("want_preview_svg", "Aperçu colorié .svg"), ("want_poster", "Poster PNG"),
                                        ("want_order_json", "Fiche JSON (sans prix)"),
                                        ("want_order_txt", "Fiche texte lisible"),
                                        ("want_colors_jpg", "Tableau couleurs JPG")] if getattr(sup, flag, False)],
        "unassigned": erp.unassigned_orders().count(), "erp_section": "suppliers"})


SUPPLIER_SECTIONS = {
    "profile": ["name", "checkout", "priority", "lead_time_days", "incoterms", "production_notes", "terms"],
    "contact": ["contact_name", "contact_email", "phone", "address", "bank_name", "iban", "bic", "notes"],
    "channel": ["email", "api_url", "api_key"],
    "dispatch": ["auto_dispatch", "want_template_tiff", "want_poster", "want_order_json", "want_order_txt", "want_colors_jpg", "want_template_pdf",
                 "want_template_svg", "want_preview_svg"],
    "excel": [], "palette": [], "docs": [],
}
SUPPLIER_SECTION_EXTRAS = {"channel": (), "excel": ("xlsx",), "palette": ("palette",), "docs": ("files",)}
SUPPLIER_LABELS = {
    "name": "Nom", "checkout": "Produit fourni", "priority": "Priorité", "lead_time_days": "Délai de production (jours)",
    "incoterms": "Incoterm", "production_notes": "Consignes de production", "terms": "Conditions commerciales",
    "contact_name": "Interlocuteur", "contact_email": "E-mail", "phone": "Téléphone", "address": "Adresse",
    "bank_name": "Banque", "iban": "IBAN", "bic": "BIC / SWIFT", "notes": "Notes internes",
    "email": "E-mail de réception des commandes", "api_url": "URL de l'API", "api_key": "Clé API",
    "auto_dispatch": "Transmettre automatiquement chaque commande payée",
    "want_template_tiff": "Toile numérotée (TIFF)", "want_poster": "Poster couleur (PNG)",
    "want_order_json": "Fiche couleurs (JSON)",
    "want_order_txt": "Fiche texte lisible (commande, destinataire, couleurs, EN/中文)",
    "want_colors_jpg": "Tableau des couleurs en image (code usine, n° sur la toile, pastille)", "want_template_pdf": "Toile numérotée (PDF)",
    "want_template_svg": "Toile numérotée (SVG)", "want_preview_svg": "Aperçu colorié (SVG)", "want_source": "Photo d'origine",
}


def _supplier_section_form(section):
    from django import forms
    from .models import Supplier
    names = SUPPLIER_SECTIONS[section]
    widgets = {k: forms.Textarea(attrs={"rows": 3}) for k in ("address", "terms", "production_notes", "notes") if k in names}
    Form = forms.modelform_factory(Supplier, fields=names, widgets=widgets, labels=SUPPLIER_LABELS) if names else \
        forms.modelform_factory(Supplier, fields=[])
    for k in ("priority",):
        if k in names:
            Form.base_fields[k].help_text = "0 = fournisseur principal"
    return Form


def _supplier_attention(sup):
    """Points a traiter sur la fiche, du plus urgent au moins urgent : [(niveau, texte, onglet)]."""
    from . import integrations
    out = []
    if not sup.active:
        out.append(("warn", "Fournisseur en pause : aucune commande ne lui est transmise.", ""))
    if sup.last_sync_at and not sup.last_sync_ok:
        out.append(("bad", "Dernière transmission en échec : %s" % (sup.last_sync_error or "erreur inconnue"), "integration"))
    n = len([x for x in integrations.pending_retries() if x.supplier_id == sup.pk])
    if n:
        out.append(("bad", "%d commande%s en échec de transmission à traiter." % (n, "s" if n > 1 else ""), "integration"))
    if sup.integration == "email" and not sup.email or sup.integration == "api" and not sup.api_url \
            or sup.integration == "woo" and not (sup.woo or {}).get("ck"):
        out.append(("bad", "Canal d'intégration incomplet.", "integration"))
    if not sup.grid():
        out.append(("warn", "Grille tarifaire vide : coûts et marges non calculés.", "tarifs"))
    else:
        g = _grid_view(sup)
        if g["missing_zones"]:
            out.append(("warn", "Livraison non chiffrée : %s." % ", ".join(g["missing_zones"]), "tarifs"))
    return out


@staff_member_required
def erp_supplier_edit(request, pk=None):
    """Creation d'un fournisseur (nom + produit) ; l'ancienne page « Modifier » renvoie vers la fiche unique."""
    from django.contrib import admin as _admin
    from .models import Supplier
    if pk:
        return redirect("studio:erp_supplier", pk=pk)
    Form = _supplier_section_form("profile")
    form = Form(request.POST or None, initial={"checkout": "kit", "lead_time_days": 5, "priority": 0})
    if request.method == "POST" and form.is_valid():
        sup = form.save()
        return redirect(reverse("studio:erp_supplier", args=[sup.pk]) + "#integration")
    return render(request, "admin/erp_supplier_new.html", {**_admin.site.each_context(request), "form": form,
                                                          "erp_section": "suppliers"})


def _save_supplier_extras(request, sup, parts=()):
    """Parties hors modele d'une section de la fiche : modele Excel (xlsx), Shopify, nuancier CSV, pieces jointes."""
    from .models import COLOR_COUNTS, SupplierFile
    P = request.POST
    notes = []

    def cost(name):
        try:
            v = (P.get(name) or "").replace(",", ".").strip()
            return round(float(v), 2) if v else None
        except ValueError:
            return None
    xl = dict((sup.offer or {}).get("xlsx") or {})
    sup.offer = dict(sup.offer or {})     # grille tarifaire : editee sur la fiche (_grid_save), pas ici
    # Pont Excel (ERP du fournisseur : MangoERP...) : modele, correspondance des colonnes, SKU
    tf = request.FILES.get("xlsx_template")
    if "xlsx" not in parts:
        pass
    elif P.get("xlsx_remove"):
        xl = {}
        notes.append("Modèle Excel retiré.")
    elif tf:
        from . import xlsx_bridge
        rel = "private/suppliers/%d/erp_template.xlsx" % sup.pk
        dst = os.path.join(settings.MEDIA_ROOT, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as out:
            for chunk in tf.chunks():
                out.write(chunk)
        try:
            headers, hrow = xlsx_bridge.read_template(dst)
        except Exception:
            headers, hrow = [], 1
        if headers:
            xl = {"template": rel, "name": tf.name[:120], "headers": headers, "header_row": hrow,
                  "map": {str(i): k for i, k in xlsx_bridge.auto_map(headers).items()},
                  "sku": xl.get("sku") or xlsx_bridge.DEFAULT_SKU, "attach": xl.get("attach", True)}
            notes.append("Modèle Excel lu : %d colonnes, %d associées automatiquement (vérifiez la correspondance)."
                         % (len(headers), len(xl["map"])))
        else:
            notes.append("Modèle Excel illisible (.xlsx attendu, avec une ligne d'en-têtes).")
    elif xl:
        xl["map"] = {str(i): P.get("xmap_%d" % i, "") for i in range(len(xl.get("headers") or []))
                     if P.get("xmap_%d" % i)}
        xl["sku"] = (P.get("xlsx_sku") or "").strip()[:80] or xl.get("sku") or ""
        xl["attach"] = bool(P.get("xlsx_attach"))
        xl["ioss"] = (P.get("xlsx_ioss") or "").strip()[:30]
        xl["logistics"] = (P.get("xlsx_logistics") or "").strip()[:60]
    if xl:
        sup.offer["xlsx"] = xl
    else:
        sup.offer.pop("xlsx", None)
    fields = ["offer"]
    if "palette" in parts and P.get("palette_clear"):
        sup.palette, sup.palette_updated_at = [], None
        fields += ["palette", "palette_updated_at"]
        notes.append("Nuancier retiré.")
    f = request.FILES.get("palette_csv") if "palette" in parts else None
    if f:
        from . import palette as _pal
        cols, rep = _pal.parse_csv(f.read())
        if cols:
            sup.palette, sup.palette_updated_at = cols, timezone.now()
            fields += ["palette", "palette_updated_at"]
            notes.append("Nuancier importé : %d teintes%s." % (len(cols), (" (%d doublon(s) ignoré(s))" % len(rep["duplicates"]))
                                                               if rep["duplicates"] else ""))
        else:
            notes.append("Nuancier non importé : 1re colonne = référence, 2e = code #RRGGBB.")
    sup.save(update_fields=list(dict.fromkeys(fields)))
    if "files" not in parts:
        return notes
    for fid in P.getlist("file_delete"):
        sf = SupplierFile.objects.filter(pk=fid, supplier=sup).first()
        if sf:
            sf.file.delete(save=False); sf.delete()
    n = 0
    for up in request.FILES.getlist("attachments")[:10]:
        if up.size > 20 * 1024 * 1024:
            notes.append("%s ignoré (> 20 Mo)." % up.name); continue
        SupplierFile.objects.create(supplier=sup, file=up, name=up.name[:160], size=up.size); n += 1
    if n:
        notes.append("%d pièce(s) jointe(s) ajoutée(s)." % n)
    return notes


@staff_member_required
def erp_supplier_export(request, pk, kind):
    """Excel au format de l'ERP du fournisseur (commandes a traiter) ou releve des montants dus."""
    from .models import Supplier
    from . import xlsx_bridge
    sup = Supplier.objects.filter(pk=pk).first()
    if not sup:
        raise Http404
    qs = Order.objects.filter(supplier=sup)
    stamp = timezone.now().strftime("%Y%m%d-%H%M")
    if kind == "orders":
        ids = [int(x) for x in request.GET.getlist("o") if x.isdigit()]
        qs = qs.filter(pk__in=ids) if ids else qs.filter(status__in=(Order.PAID, Order.FULFILLED))
        data = xlsx_bridge.build_xlsx(sup, list(qs.order_by("created_at")))
        name = "paintit-commandes-%s.xlsx" % stamp
    elif kind == "statement":
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "releve"
        pay = sup.payments.filter(pk=request.GET.get("pay")).first() if (request.GET.get("pay") or "").isdigit() else None
        if pay:
            ws.append(["Règlement", pay.reference, "", "", "Montant", "%.2f %s" % (pay.amount_native, pay.currency)])
            ws.append([])
        ws.append(["Commande", "Date", "Produit", "Statut", "Suivi", "Montant dû (EUR)"])
        rows = (pay.orders.all() if pay else qs.filter(status__in=(Order.FULFILLED, Order.PRODUCTION, Order.SHIPPED, Order.DELIVERED),
                                                         supplier_paid_at__isnull=True)).order_by("created_at")
        tot = 0.0
        for o in rows:
            ws.append([o.uid, o.created_at.strftime("%Y-%m-%d"), "%s · %d couleurs%s" % (o.format_label, o.colors, " · pinceaux" if o.brushes else ""),
                       o.get_status_display(), o.tracking_number or "", round(o.cost or 0, 2)])
            tot += o.cost or 0
        ws.append([]); ws.append(["", "", "", "", "TOTAL", round(tot, 2)])
        for col, w in zip("ABCDEF", (18, 12, 34, 22, 20, 16)):
            ws.column_dimensions[col].width = w
        import io as _io
        buf = _io.BytesIO(); wb.save(buf); data = buf.getvalue()
        name = "paintit-releve-%s-%s.xlsx" % (_slug(sup.name), stamp)
    else:
        raise Http404
    resp = HttpResponse(data, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = 'attachment; filename="%s"' % name
    return resp


@staff_member_required
def erp_supplier_file(request, pk, fid):
    from .models import SupplierFile
    sf = SupplierFile.objects.filter(pk=fid, supplier_id=pk).first()
    if not sf or not sf.file:
        raise Http404
    try:
        return FileResponse(sf.file.open("rb"), as_attachment=False, filename=sf.name)
    except OSError:
        raise Http404


# ---------------- Catalogue (galerie de modeles + toiles clients) ----------------
LIB_EMAIL = "__library__"


def _slug(text):
    import re as _re, unicodedata
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return _re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40] or "modele"


def _create_gallery_model(title, category="", price=0, colors=24, orientation="portrait",
                          image_path=None, from_uid=None, in_slider=False, zones=(None, 1.0)):
    """Ajout a la galerie = exactement `seed_library` : l'image est rangee dans
    studio/static/studio/gallery/<Categorie>/ (convention de nommage) puis seedee
    (toile gal-..., fiche bibliotheque, assets du home). Source : image_path, ou la photo
    originale de la toile `from_uid`."""
    from .pipeline import source_file
    from .management.commands.seed_library import seed_image
    if from_uid and not image_path:
        image_path = source_file(os.path.join(settings.MEDIA_ROOT, "orders", from_uid), from_uid) or None
        if not image_path:
            raise ValueError("photo originale de cette toile non conservée : impossible de la seeder")
    m = seed_image(image_path, title, category, price, colors=int(colors or 24), landscape=(orientation == "paysage"),
                   in_slider=in_slider, density=zones[0], detail=zones[1])
    if from_uid:
        DigitalCanvas.objects.filter(pk=m.pk).update(origin_uid=from_uid)
    return m


@staff_member_required
def erp_catalogue(request):
    from decimal import Decimal, InvalidOperation
    from django.contrib import admin as _admin
    from django.contrib import messages
    from django.db.models import Count, Q
    from .models import DigitalCanvas
    lib = DigitalCanvas.objects.filter(email=LIB_EMAIL)
    if request.method == "POST":
        op = request.POST.get("op")
        m = lib.filter(pk=request.POST.get("id")).first()

        def density():
            """'densite|detail' (ex. '0.7|0.75') -> (densite zones/cm2, finesse des zones) ; vide = auto."""
            try:
                d, _, det = (request.POST.get("density") or "").replace(",", ".").partition("|")
                return min(5.0, max(0.02, float(d))), (min(2.0, max(0.4, float(det))) if det else 1.0)
            except ValueError:
                return None, 1.0

        def price(v):
            try:
                return max(Decimal("0"), Decimal((v or "0").replace(",", ".")).quantize(Decimal("0.01")))
            except InvalidOperation:
                return Decimal("0")
        if op == "update" and m:
            m.title = (request.POST.get("title") or "").strip()[:80]
            m.category = (request.POST.get("category") or "").strip()[:40]
            m.price = price(request.POST.get("price"))
            m.save(update_fields=["title", "category", "price"])
            messages.success(request, "« %s » mis à jour." % (m.title or m.uid))
        elif op == "hide" and m:
            m.hidden = not m.hidden
            m.save(update_fields=["hidden"])
            messages.success(request, "« %s » %s." % (m.title or m.uid, "retiré temporairement de la galerie (design conservé)"
                                                    if m.hidden else "remis en ligne dans la galerie"))
        elif op == "slider" and m:
            m.in_slider = not m.in_slider
            m.save(update_fields=["in_slider"])
            messages.success(request, "« %s » %s le slider de l'accueil." % (m.title or m.uid,
                                                                            "ajouté dans" if m.in_slider else "retiré du"))
        elif op == "unpublish" and m:
            from .management.commands.seed_library import retire_model
            retire_model(m.uid)
            m.delete()   # les fichiers restent : le modele peut etre republie depuis « Toiles clients »
            messages.success(request, "Modèle retiré de la galerie (les joueurs le conservent ; image archivée dans gallery/_retires/).")
        elif op == "delete" and m:
            import shutil
            from .management.commands.seed_library import retire_model
            uid = m.uid
            retire_model(uid)
            owners = DigitalCanvas.objects.filter(uid=uid).exclude(email=LIB_EMAIL).count()
            m.delete()
            if not owners and not Order.objects.filter(uid=uid).exists():
                shutil.rmtree(os.path.join(settings.MEDIA_ROOT, "orders", uid), ignore_errors=True)
            messages.success(request, "Modèle supprimé%s." % (" (fichiers conservés : %d joueur(s) le possèdent)" % owners
                                                             if owners else ""))
        elif op == "publish":
            c = DigitalCanvas.objects.filter(pk=request.POST.get("id")).exclude(email=LIB_EMAIL).first()
            if c and lib.filter(origin_uid=c.uid).exists():
                messages.error(request, "Cette toile est déjà publiée dans la galerie.")
            elif c:
                try:
                    orient = c.orientation if c.orientation in ("portrait", "paysage") else \
                        ("paysage" if (c.width_cm or 40) > (c.height_cm or 50) else "portrait")
                    m = _create_gallery_model((request.POST.get("title") or c.title or "Nouveau modèle").strip(),
                                              request.POST.get("category") or c.category, price(request.POST.get("price")),
                                              colors=c.colors or 24, orientation=orient, from_uid=c.uid,
                                              in_slider=bool(request.POST.get("in_slider")), zones=density())
                    messages.success(request, "Toile publiée dans la galerie : modèle « %s » (%s)." % (m.title, m.uid))
                except Exception as exc:
                    logger.exception("Publication galerie %s", c.uid)
                    messages.error(request, "Publication impossible : %s" % exc)
        elif op == "add":
            from . import imagebank
            from .models import StockImage
            f = request.FILES.get("image")
            stock = None if f else StockImage.objects.filter(pk=request.POST.get("stock_id") or 0).first()
            title = (request.POST.get("title") or "").strip()[:80]
            if not (f or stock) or not title:
                messages.error(request, "Image et titre obligatoires.")
            else:
                try:
                    if stock:                                # image de la banque : HD telechargee a l'usage
                        tmp = imagebank.fetch_hd(stock)
                    else:
                        tmp = os.path.join(settings.MEDIA_ROOT, "uploads", "gal_%s%s" % (uuid.uuid4().hex[:8],
                                           os.path.splitext(f.name)[1] or ".jpg"))
                        os.makedirs(os.path.dirname(tmp), exist_ok=True)
                        with open(tmp, "wb") as out:
                            for chunk in f.chunks():
                                out.write(chunk)
                    m = _create_gallery_model(title, request.POST.get("category", "").strip(), price(request.POST.get("price")),
                                          colors=int(request.POST.get("colors") or 24),
                                          orientation=request.POST.get("orientation", "portrait"),
                                          image_path=tmp, in_slider=bool(request.POST.get("in_slider")), zones=density())
                    os.remove(tmp)
                    if stock:
                        imagebank.mark_used(stock, "catalogue " + m.uid)
                    messages.success(request, "Modèle « %s » créé et publié%s." % (
                        title, " (galerie + slider de l'accueil)" if request.POST.get("in_slider") else " dans la galerie"))
                except PermissionError as exc:
                    logger.exception("Catalogue add")
                    messages.error(request, "Génération impossible : le site n'a pas le droit d'écrire dans %s. "
                                   "Sur le serveur : sudo chown -R paintit:paintit %s" % (
                                       os.path.dirname(getattr(exc, "filename", "") or "") or "media/", settings.MEDIA_ROOT))
                except Exception as exc:
                    logger.exception("Catalogue add")
                    messages.error(request, "Génération impossible : %s" % exc)
        return redirect(request.get_full_path())

    tab = request.GET.get("tab", "gallery")
    q = (request.GET.get("q") or "").strip()
    cat = request.GET.get("cat", "")
    pf = request.GET.get("price", "")
    sort = request.GET.get("sort", "recent")
    owners = dict(DigitalCanvas.objects.exclude(email=LIB_EMAIL).values_list("uid").annotate(n=Count("id")))
    cats = sorted(set(c for c in lib.values_list("category", flat=True) if c))
    stats = {"models": lib.count(), "free": lib.filter(price=0).count(), "paid": lib.filter(price__gt=0).count(),
             "players": sum(owners.get(u, 0) for u in lib.values_list("uid", flat=True)),
             "sales": sum(owners.get(u, 0) for u in lib.filter(price__gt=0).values_list("uid", flat=True)),
             "customer": DigitalCanvas.objects.exclude(email=LIB_EMAIL).exclude(source="library").count(),
             "slider": lib.filter(in_slider=True).count(), "hidden": lib.filter(hidden=True).count()}
    from .management.commands.purge_old_data import production_expired
    stats["purge_due"] = sum(1 for _ in production_expired(60))
    stats["revenue"] = round(sum(float(p) * owners.get(u, 0) for u, p in lib.filter(price__gt=0)
                                 .values_list("uid", "price")), 2)
    if tab == "customer":
        qs = DigitalCanvas.objects.exclude(email=LIB_EMAIL).exclude(source="library")
        if q:
            qs = qs.filter(Q(email__icontains=q) | Q(uid__icontains=q) | Q(title__icontains=q))
        published = set(lib.values_list("uid", flat=True)) | set(lib.exclude(origin_uid="").values_list("origin_uid", flat=True))
        items = list(qs.order_by("-created_at")[:120])
        for c in items:
            c.published = c.uid in published
    else:
        qs = lib
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(uid__icontains=q) | Q(category__icontains=q))
        if cat:
            qs = qs.filter(category=cat)
        if request.GET.get("slider") == "1":
            qs = qs.filter(in_slider=True)
        if pf == "free":
            qs = qs.filter(price=0)
        elif pf == "paid":
            qs = qs.filter(price__gt=0)
        items = list(qs.order_by("-created_at" if sort == "recent" else "title"))
        for m in items:
            m.players = owners.get(m.uid, 0)
        if sort == "popular":
            items.sort(key=lambda m: -m.players)
    from .pipeline import source_file
    shows = set()
    for folder in (os.path.join(settings.BASE_DIR, "studio", "static", "studio", "showcase"),
                   os.path.join(settings.MEDIA_ROOT, "showcase")):
        if os.path.isdir(folder):
            shows.update(f[:-8] for f in os.listdir(folder) if f.endswith("_pbn.png"))
    for it in items:
        it.has_showcase = bool(getattr(it, "showcase_slug", "")) and it.showcase_slug in shows
    for it in items:   # original perdu (bug de regeneration corrige) : pas de lien mort
        it.has_source = bool(source_file(os.path.join(settings.MEDIA_ROOT, "orders", it.uid), it.uid))
    return render(request, "admin/erp_catalogue.html", {
        **_admin.site.each_context(request), "tab": tab, "items": items, "q": q, "cat": cat, "pf": pf,
        "sort": sort, "cats": cats, "stats": stats, "slider": request.GET.get("slider", ""),
        "bank": _bank_picks(), "erp_section": "catalogue",
        "cat_suggest": sorted(set(cats) | set(__import__("studio.categories", fromlist=["x"]).names())),
        "untranslated": [c for c in cats if not __import__("studio.categories", fromlist=["x"]).known(c)]})


def _zones(v):
    """'densite|detail' (ex. '0.7|0.75') -> (densite zones/cm2, finesse) ; vide ou invalide = auto."""
    try:
        d, _, det = (v or "").replace(",", ".").partition("|")
        return min(5.0, max(0.02, float(d))), (min(2.0, max(0.4, float(det))) if det else 1.0)
    except ValueError:
        return None, 1.0


def _bank_picks(limit=150):
    """Images de la banque proposees dans « Nouveau modele » : retenues d'abord, puis les plus colorees."""
    from .models import StockImage
    qs = StockImage.objects.filter(status__in=("new", "kept"), people=False)
    kept = list(qs.filter(status="kept").order_by("-vibrancy")[:limit])
    return kept + list(qs.filter(status="new").order_by("-vibrancy")[:max(0, limit - len(kept))])



@csrf_exempt
def supplier_webhook(request, secret):
    """Retours automatiques des fournisseurs (statut, suivi). Voir studio/integrations.py."""
    import json as _json
    from .models import Supplier
    from . import integrations
    sup = Supplier.objects.filter(webhook_secret=secret).first() if secret else None
    if not sup or not sup.active:
        return JsonResponse({"error": "fournisseur inconnu"}, status=404)
    if request.method != "POST":
        return JsonResponse({"supplier": sup.name, "usage": "POST JSON {uid, status, carrier, tracking_number, "
                             "tracking_url, supplier_ref, message}"})
    sig = request.headers.get("X-PaintIt-Signature", "")
    if sig and not __import__("hmac").compare_digest(sig, integrations.sign(sup, request.body)):
        return JsonResponse({"error": "signature invalide"}, status=401)
    try:
        data = _json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"error": "JSON invalide"}, status=400)
    events = data if isinstance(data, list) else [data]
    results = []
    for ev in events[:100]:
        code, msg = integrations.handle_event(sup, ev if isinstance(ev, dict) else {})
        results.append({"uid": (ev or {}).get("uid") if isinstance(ev, dict) else None, "status": code, "message": msg})
    status = 200 if all(r["status"] == 200 for r in results) else (results[0]["status"] if len(results) == 1 else 207)
    return JsonResponse({"results": results}, status=status)


# ---------------- Fiche commande (ERP) ----------------
def _order_files(o):
    """Tous les fichiers de la commande (+ variante de jeu <uid>-G), groupes par usage."""
    groups = [("visuals", "Visuels"), ("print", "Impression / fournisseur"), ("data", "Données"),
              ("game", "Jeu en ligne"), ("other", "Autres")]
    out = {k: [] for k, _ in groups}
    for folder in (o.uid, o.uid + "-G"):
        d = os.path.join(settings.MEDIA_ROOT, "orders", folder)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            low = name.lower()
            ext = low.rsplit(".", 1)[-1] if "." in low else ""
            if folder.endswith("-G"):
                g = "game"
            elif "_digipaint" in low:
                g = "game"
            elif ext in ("tiff", "tif") or "_poster" in low or "_supplier." in low or \
                    (ext in ("svg", "pdf") and "_template" in low):
                g = "print"
            elif ext == "json":
                g = "data"
            elif ext in ("png", "jpg", "jpeg", "webp", "svg"):
                g = "visuals"
            else:
                g = "other"
            label = {"_source_": "Photo originale", "_preview.": "Toile coloriée", "_template.": "Toile numérotée",
                     "_poster": "Poster (notice + palette)", "_palette": "Palette", "_order.json": "Bon de commande interne (order.json)",
                     "_colors.json": "Couleurs (JSON)", "_supplier.json": "Fiche fournisseur (sans prix)", "_supplier.txt": "Fiche fournisseur texte (EN/中文)", "_supplier_colors.jpg": "Tableau des couleurs (JPG)",
                     "_template.pdf": "Toile numérotée PDF vectoriel", "_template.tiff": "Toile numérotée TIFF 600 dpi", "_digipaint": "Toile jouable", "_preview_": "Aperçu"}
            hits = [k for k in label if k in low]
            nice = label[max(hits, key=len)] if hits else name   # cle la plus specifique
            out[g].append({"name": name, "folder": folder, "label": nice, "ext": ext.upper(),
                           "size": os.path.getsize(p), "mtime": os.path.getmtime(p),
                           "image": ext in ("png", "jpg", "jpeg", "webp") or (ext == "svg" and "_digipaint" not in low)})
    return [(k, lbl, out[k]) for k, lbl in groups if out[k]]


def _erp_order_or_404(pk):
    o = Order.objects.select_related("supplier").filter(pk=pk).first()
    if not o:
        raise Http404
    return o


@staff_member_required
def erp_order_file(request, pk, folder, name):
    o = _erp_order_or_404(pk)
    import re as _re
    if folder not in (o.uid, o.uid + "-G") or not _re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise Http404
    p = os.path.join(settings.MEDIA_ROOT, "orders", folder, name)
    if not os.path.isfile(p):
        raise Http404
    import mimetypes
    resp = FileResponse(open(p, "rb"), content_type=mimetypes.guess_type(name)[0] or "application/octet-stream")
    if request.GET.get("dl"):
        resp["Content-Disposition"] = 'attachment; filename="%s"' % name
    return resp


@staff_member_required
def erp_order_receipt(request, pk):
    o = _erp_order_or_404(pk)
    resp = HttpResponse(receipts.build_receipt(o), content_type="application/pdf")
    resp["Content-Disposition"] = '%s; filename="PaintIt-%s.pdf"' % ("attachment" if request.GET.get("dl") else "inline", o.uid)
    return resp


@staff_member_required
def erp_order_zip(request, pk):
    import io, zipfile
    o = _erp_order_or_404(pk)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for _k, _l, files in _order_files(o):
            for f in files:
                z.write(os.path.join(settings.MEDIA_ROOT, "orders", f["folder"], f["name"]),
                        ("jeu/" if f["folder"].endswith("-G") else "") + f["name"])
        try:
            z.writestr("PaintIt-%s-recu.pdf" % o.uid, receipts.build_receipt(o))
        except Exception:
            logger.exception("Recu zip %s", o.uid)
    resp = HttpResponse(buf.getvalue(), content_type="application/zip")
    resp["Content-Disposition"] = 'attachment; filename="PaintIt-%s.zip"' % o.uid
    return resp


def _delete_orders(qs):
    """Supprime des commandes (journal compris) et leurs fichiers de production.
    Les fichiers restent s'ils servent encore a une toile de « Mes toiles »."""
    import shutil
    n = 0
    for o in qs:
        if not DigitalCanvas.objects.filter(uid=o.uid).exists():
            for u in (o.uid, o.uid + "-G"):
                shutil.rmtree(os.path.join(settings.MEDIA_ROOT, "orders", u), ignore_errors=True)
        o.delete()
        n += 1
    return n


def _delete_customer(email):
    """Droit a l'effacement (RGPD) : commandes, toiles numeriques, messages et codes de connexion du client."""
    from .models import ContactMessage
    if not email or email == "__library__":
        return 0
    n = _delete_orders(Order.objects.filter(customer_email__iexact=email))
    DigitalCanvas.objects.filter(email__iexact=email).delete()
    EmailCode.objects.filter(email__iexact=email).delete()
    ContactMessage.objects.filter(email__iexact=email).delete()
    return n


@staff_member_required
def erp_order(request, pk):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from .models import Supplier
    from . import erp
    o = _erp_order_or_404(pk)
    user = request.user.get_username()

    def log(kind, text):
        OrderEvent.objects.create(order=o, kind=kind, text=text[:300], user=user)
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "delete":
            _delete_orders(Order.objects.filter(pk=o.pk))
            messages.success(request, "Commande %s supprimée." % o.uid)
            return redirect("studio:erp_orders")
        try:
            if op == "status":
                st = request.POST.get("status")
                if st in dict(Order.STATUS_CHOICES) and st != o.status:
                    o.status = st; o._erp_user = user
                    o.save(update_fields=["status", "status_changed_at"])
                    if st == Order.DELIVERED and not o.feedback_sent:
                        emails.send_feedback_request(o)
                        Order.objects.filter(pk=o.pk).update(feedback_sent=True)
                        log("email", "Demande d'avis envoyée")
                    messages.success(request, "Statut : %s." % o.get_status_display())
            elif op == "dispatch":
                sup = Supplier.objects.filter(pk=request.POST.get("supplier")).first() or o.supplier
                ok = _notify_supplier(_order_from_row(o), _shipping_from_row(o), sup=sup, user=user)
                if ok and o.status == Order.PAID:
                    o.status = Order.FULFILLED; o._erp_user = user
                    o.save(update_fields=["status", "status_changed_at"])
                (messages.success if ok else messages.error)(
                    request, "Commande transmise." if ok else "Échec de transmission : voir l'historique.")
            elif op == "tracking":
                for f in ("carrier", "tracking_number", "tracking_url"):
                    setattr(o, f, (request.POST.get(f) or "").strip()[:300])
                fields = ["carrier", "tracking_number", "tracking_url"]
                if request.POST.get("ship") and o.status in (Order.PAID,) + Order.AT_SUPPLIER:
                    o.status = Order.SHIPPED; o._erp_user = user; fields += ["status", "status_changed_at"]
                o.save(update_fields=fields)
                log("action", "Suivi : %s %s" % (o.carrier, o.tracking_number))
                messages.success(request, "Suivi enregistré.")
            elif op == "parcel_check":
                from . import parcel
                if not parcel.enabled():
                    messages.error(request, "Suivi automatique inactif : ajoutez PARCELSAPP_API_KEY (ou AFTERSHIP_API_KEY) dans .env.")
                else:
                    messages.info(request, "Suivi : %s" % parcel.check(o, force=True))
            elif op == "note":
                txt = (request.POST.get("text") or "").strip()
                if txt:
                    log("note", txt)
            elif op == "notes":
                o.notes = request.POST.get("notes", ""); o.save(update_fields=["notes"])
                messages.success(request, "Notes internes enregistrées.")
            elif op == "missing":
                missing = ensure_supplier_files(_order_from_row(o), _shipping_from_row(o), user=user)
                (messages.error if missing else messages.success)(
                    request, ("Toujours manquant : %s (voir l'historique)." % ", ".join(missing)) if missing
                    else "Dossier fournisseur complet.")
            elif op == "tiff":
                from .pipeline import export_tiff
                made = export_tiff(o.uid)
                log("action", "Fichiers TIFF générés (%d)" % len(made))
                messages.success(request, "%d fichier(s) TIFF généré(s)." % len(made))
            elif op == "manifest":
                fulfillment.build(_order_from_row(o), _shipping_from_row(o))
                log("action", "Poster + order.json régénérés")
                messages.success(request, "Poster et order.json régénérés.")
            elif op == "confirmation":
                emails.send_order_confirmation(_order_from_row(o), _shipping_from_row(o))
                log("email", "Confirmation de commande renvoyée au client")
                messages.success(request, "E-mail de confirmation renvoyé.")
            elif op == "feedback":
                emails.send_feedback_request(o)
                Order.objects.filter(pk=o.pk).update(feedback_sent=True)
                log("email", "Demande d'avis envoyée")
                messages.success(request, "Demande d'avis envoyée.")
        except Exception as exc:
            logger.exception("Fiche commande %s op=%s", o.uid, op)
            messages.error(request, "Action impossible : %s" % exc)
        return redirect("studio:erp_order", pk=o.pk)

    files = _order_files(o)
    from .pipeline import source_file
    d = os.path.join(settings.MEDIA_ROOT, "orders", o.uid)
    visuals = [(k, lbl) for k, lbl in (("source", "Photo originale"), ("preview", "Toile coloriée"),
                                       ("template", "Toile numérotée"), ("poster", "Poster"))]
    have = {"source": bool(source_file(d, o.uid)),
            "preview": any(os.path.exists(os.path.join(d, "%s_preview.%s" % (o.uid, e))) for e in ("png", "svg")),
            "template": any(os.path.exists(os.path.join(d, "%s_template.%s" % (o.uid, e))) for e in ("png", "svg")),
            "poster": os.path.exists(os.path.join(d, "%s_poster.png" % o.uid))}
    flags = [lbl for key, (qs, lbl, lvl, hint) in erp.action_querysets().items() if qs.filter(pk=o.pk).exists()]
    others = Order.objects.filter(customer_email=o.customer_email).exclude(pk=o.pk).order_by("-created_at")[:5]
    ad = None
    if o.ad_ref:
        from .models import MarketingAd
        ad = MarketingAd.objects.filter(token=o.ad_ref).first()
    sale = o.ledger.filter(kind="sale").first()
    fee = sum(e.fee for e in o.ledger.all())
    fin = {"sale": sale, "fee": fee,
           "margin": round(float(o.total or 0) - float(o.refunded_amount or 0) - fee - float(o.cost or 0), 2)}
    # Frise : Payee -> Envoyee -> En production -> Expediee -> Livree (date du 1er passage par chaque etape)
    first = {}
    for ev in o.events.filter(kind="status").order_by("at"):
        first.setdefault(ev.status, ev.at)
    flow = list(Order.FLOW[1:])
    cur = flow.index(o.status) if o.status in flow else -1
    labels = dict(Order.STATUS_CHOICES)
    track = [{"label": labels[s], "at": first.get(s) or (o.created_at if s == Order.PAID and cur >= 0 else None),
              "done": i <= cur, "current": i == cur} for i, s in enumerate(flow)]
    return render(request, "admin/erp_order.html", {"paid_statuses": Order.PAID_STATUSES, "fin": fin, "track": track,
        **_admin.site.each_context(request), "o": o, "files": files, "visuals": visuals, "have": have, "sfiles": supplier_files_status(o.uid),
        "sfiles_ok": all(f["ok"] for f in supplier_files_status(o.uid)),
        "has_tiff": any(f["ext"] in ("TIFF", "TIF") for _k, _l, fs in files for f in fs),
        "has_json": any(f["name"].endswith("_order.json") for _k, _l, fs in files for f in fs),
        "events": o.events.all()[:60], "flags": flags, "others": others, "ad": ad,
        "statuses": Order.STATUS_CHOICES, "suppliers": Supplier.objects.all(),
        "age": (timezone.now() - (o.status_changed_at or o.created_at)).days,
        "erp_section": "orders"})


# ---------------- Liste des commandes (ERP) ----------------
ORDER_TABS = [("todo", "À traiter"), ("pending", "En attente"), ("paid", "Payées"), ("fulfilled", "Envoyées"),
              ("production", "En production"),
              ("shipped", "Expédiées"), ("delivered", "Livrées"), ("failed", "Échecs"), ("all", "Toutes")]


def _orders_queryset(request):
    import datetime as _dt
    from django.db.models import Q
    from . import erp
    g = request.GET
    tab = g.get("tab") or ("todo" if not (g.get("status") or g.get("action")) else "all")
    qs = Order.objects.select_related("supplier")
    if g.get("action"):                                   # liens du Centre d'actions
        aq = erp.action_querysets().get(g["action"])
        if aq:
            qs = qs.filter(pk__in=aq[0].values("pk"))
    if g.get("status"):
        qs = qs.filter(status=g["status"])
    if tab == "todo":
        ids = set()
        for q_, *_rest in erp.action_querysets().values():
            ids.update(q_.values_list("pk", flat=True))
        qs = qs.filter(pk__in=ids)
    elif tab in dict(Order.STATUS_CHOICES):
        qs = qs.filter(status=tab)
    q = (g.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(uid__icontains=q) | Q(customer_name__icontains=q) | Q(customer_email__icontains=q) |
                       Q(city__icontains=q) | Q(tracking_number__icontains=q) | Q(supplier_ref__icontains=q))
    if g.get("supplier"):
        qs = qs.filter(supplier__isnull=True) if g["supplier"] == "none" else qs.filter(supplier_id=g["supplier"])
    if g.get("country"):
        qs = qs.filter(country=g["country"])
    days = {"7d": 7, "30d": 30, "90d": 90}.get(g.get("period", ""))
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - _dt.timedelta(days=days))
    sort = g.get("sort") or "-created_at"
    allowed = {"created_at", "total", "status", "customer_name", "status_changed_at", "uid"}
    if sort.lstrip("-") in allowed:
        qs = qs.order_by(sort, "-id")
    return qs, tab, sort


@staff_member_required
def erp_orders(request):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from django.core.paginator import Paginator
    from django.db.models import Count, Sum
    from .models import Supplier
    from . import erp
    user = request.user.get_username()
    if request.method == "POST":
        ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
        op = request.POST.get("op")
        sel = Order.objects.filter(pk__in=ids)
        if not ids:
            messages.error(request, "Sélectionnez au moins une commande.")
        elif op == "csv":
            return _csv_response("commandes_selection.csv",
                                 ["uid", "date", "statut", "format", "couleurs", "total_eur", "marge_eur", "client",
                                  "email", "ville", "pays", "fournisseur", "suivi"],
                                 ([o.uid, o.created_at.strftime("%Y-%m-%d %H:%M"), o.get_status_display(), o.format_label,
                                   o.colors, o.total, o.benefit, o.customer_name, o.customer_email, o.city, o.country,
                                   o.supplier.name if o.supplier else "", o.tracking_number] for o in sel))
        elif op == "dispatch":
            ok = ko = 0
            for o in sel:
                if _notify_supplier(_order_from_row(o), _shipping_from_row(o), sup=o.supplier, user=user):
                    ok += 1
                    if o.status == Order.PAID:
                        o.status = Order.FULFILLED; o._erp_user = user; o.save(update_fields=["status", "status_changed_at"])
                else:
                    ko += 1
            (messages.warning if ko else messages.success)(request, "%d transmise(s), %d échec(s)." % (ok, ko))
        elif op in ("shipped", "delivered", "failed", "fulfilled", "production"):
            n = 0
            for o in sel.exclude(status=op):
                o.status = op; o._erp_user = user; o.save(update_fields=["status", "status_changed_at"]); n += 1
                if op == Order.DELIVERED and not o.feedback_sent:
                    try:
                        emails.send_feedback_request(o)
                        Order.objects.filter(pk=o.pk).update(feedback_sent=True)
                        OrderEvent.objects.create(order=o, kind="email", text="Demande d'avis envoyée", user=user)
                    except Exception:
                        logger.exception("Avis %s", o.uid)
            messages.success(request, "%d commande(s) : %s." % (n, dict(Order.STATUS_CHOICES)[op]))
        elif op == "delete":
            n = _delete_orders(sel)
            messages.success(request, "%d commande(s) supprimée(s)." % n)
        elif op == "feedback":
            n = 0
            for o in sel:
                try:
                    emails.send_feedback_request(o); n += 1
                    Order.objects.filter(pk=o.pk).update(feedback_sent=True)
                    OrderEvent.objects.create(order=o, kind="email", text="Demande d'avis envoyée", user=user)
                except Exception:
                    logger.exception("Avis %s", o.uid)
            messages.success(request, "%d demande(s) d'avis envoyée(s)." % n)
        return redirect(request.get_full_path())

    qs, tab, sort = _orders_queryset(request)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    now = timezone.now()
    p = Pricing.get()
    rows = []
    for o in page.object_list:
        age = now - (o.status_changed_at or o.created_at)
        lead = (o.supplier.lead_time_days if o.supplier else 5)
        late = (o.status == Order.PAID and age.total_seconds() > 12 * 3600) or \
               (o.status in Order.AT_SUPPLIER and age.days >= lead) or \
               (o.status == Order.SHIPPED and age.days >= int(p.delivery_days_max or 9) + 3)
        rows.append({"o": o, "age": ("%d j" % age.days) if age.days else ("%d h" % (age.seconds // 3600)), "late": late})
    counts = dict(Order.objects.values_list("status").annotate(n=Count("id")))
    todo_ids = set()
    for q_, *_r in erp.action_querysets().values():
        todo_ids.update(q_.values_list("pk", flat=True))
    tabs = [(k, lbl, len(todo_ids) if k == "todo" else sum(counts.values()) if k == "all" else counts.get(k, 0))
            for k, lbl in ORDER_TABS]
    agg = qs.aggregate(n=Count("id"), ca=Sum("total"))
    params = request.GET.copy(); params.pop("page", None)
    sparams = request.GET.copy(); sparams.pop("sort", None); sparams.pop("page", None)
    return render(request, "admin/erp_orders.html", {
        **_admin.site.each_context(request), "rows": rows, "page": page, "tab": tab, "tabs": tabs, "sort": sort,
        "q": request.GET.get("q", ""), "supplier": request.GET.get("supplier", ""),
        "country": request.GET.get("country", ""), "period": request.GET.get("period", ""),
        "suppliers": Supplier.objects.all(),
        "countries": sorted(c for c in Order.objects.values_list("country", flat=True).distinct() if c),
        "total": agg["n"] or 0, "revenue": round(agg["ca"] or 0, 2), "qs_page": params.urlencode(),
        "qs_sort": sparams.urlencode(), "action": request.GET.get("action", ""), "erp_section": "orders"})


# ---------------- Messagerie (boite de reception ERP) ----------------
CANNED_REPLIES = [
    ("Suivi de colis", "Bonjour {prenom},\n\nMerci pour votre message. Votre commande {commande} a bien été expédiée{suivi}.\n\nN'hésitez pas à revenir vers nous si besoin.\n\nBelle journée,\nL'équipe PaintIt"),
    ("Délai de production", "Bonjour {prenom},\n\nMerci pour votre patience ! Votre toile {commande} est en cours de production dans notre atelier : chaque kit est imprimé et préparé à la demande. Vous recevrez un e-mail avec le numéro de suivi dès son expédition.\n\nBelle journée,\nL'équipe PaintIt"),
    ("Retouche de la toile", "Bonjour {prenom},\n\nMerci pour votre retour. Nous pouvons tout à fait ajuster votre toile (nombre de couleurs, niveau de détail, cadrage). Pouvez-vous nous préciser ce que vous souhaitez modifier ?\n\nBelle journée,\nL'équipe PaintIt"),
    ("Produit abîmé", "Bonjour {prenom},\n\nNous sommes désolés que votre colis soit arrivé endommagé. Pourriez-vous nous envoyer une photo du colis et du contenu ? Nous vous renverrons un kit neuf sans frais dès réception.\n\nToutes nos excuses,\nL'équipe PaintIt"),
    ("Remerciement", "Bonjour {prenom},\n\nUn grand merci pour votre message, cela fait vraiment plaisir à toute l'équipe !\n\nÀ très bientôt sur PaintIt,\nL'équipe PaintIt"),
]


@staff_member_required
def erp_inbox(request, pk=None):
    import json as _json
    from django.contrib import admin as _admin
    from django.contrib import messages as flash
    from django.core.mail import EmailMessage
    from django.db.models import Q, Count
    from .models import ContactMessage, MessageReply
    user = request.user.get_username()
    cur = ContactMessage.objects.filter(pk=pk).first() if pk else None
    if pk and not cur:
        raise Http404
    if request.method == "POST" and cur:
        op = request.POST.get("op")
        thread_open = ContactMessage.objects.filter(email=cur.email, answered=False)
        if op == "reply":
            body = (request.POST.get("body") or "").strip()
            files = request.FILES.getlist("files")
            if not body and not files:
                flash.error(request, "Message vide.")
            elif sum(f.size for f in files) > 15 * 1024 * 1024:
                flash.error(request, "Pièces jointes trop lourdes (15 Mo max).")
            else:
                mail = EmailMessage("Re: %s" % (cur.subject or "Votre message"), body, settings.DEFAULT_FROM_EMAIL,
                                    [cur.email], reply_to=[settings.DEFAULT_FROM_EMAIL])
                for f in files:
                    mail.attach(f.name, f.read(), getattr(f, "content_type", None) or None)
                try:
                    sent = mail.send(fail_silently=False) > 0
                except Exception as exc:
                    logger.exception("Reponse message %s", cur.pk)
                    sent = False
                    flash.error(request, "Échec d'envoi de l'e-mail : %s" % exc)
                MessageReply.objects.create(message=cur, body=body, attachments=[f.name for f in files],
                                            user=user, sent=sent)
                if sent:
                    now = timezone.now()
                    targets = thread_open if request.POST.get("close_all") else ContactMessage.objects.filter(pk=cur.pk)
                    targets.update(answered=True, answered_at=now)
                    ContactMessage.objects.filter(pk=cur.pk).update(answer=body)
                    flash.success(request, "Réponse envoyée à %s." % cur.email)
        elif op == "close":
            thread_open.update(answered=True, answered_at=timezone.now())
            flash.success(request, "Conversation marquée comme traitée.")
        elif op == "reopen":
            ContactMessage.objects.filter(pk=cur.pk).update(answered=False)
            flash.success(request, "Conversation rouverte.")
        elif op == "delete":
            cur.delete()
            flash.success(request, "Message supprimé.")
            return redirect("studio:erp_inbox")
        elif op == "delete_thread":
            n, _ = ContactMessage.objects.filter(email__iexact=cur.email).delete()
            flash.success(request, "Conversation avec %s supprimée." % cur.email)
            return redirect("studio:erp_inbox")
        return redirect("studio:erp_inbox_msg", pk=cur.pk)

    box = request.GET.get("box", "open")
    q = (request.GET.get("q") or "").strip()
    qs = ContactMessage.objects.annotate(nfiles=Count("attachments", distinct=True)).order_by("-created_at")
    if box == "open":
        qs = qs.filter(answered=False)
    elif box == "done":
        qs = qs.filter(answered=True)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(subject__icontains=q) | Q(message__icontains=q))
    items = list(qs[:200])
    counts = {"open": ContactMessage.objects.filter(answered=False).count(),
              "done": ContactMessage.objects.filter(answered=True).count(), "all": ContactMessage.objects.count()}
    if not cur and items:
        cur = items[0]
    import datetime as _dt
    late_ids = {m.pk for m in items if not m.answered and m.created_at < timezone.now() - _dt.timedelta(hours=24)}
    ctx = {**_admin.site.each_context(request), "items": items, "box": box, "q": q, "counts": counts, "cur": cur,
           "late_ids": late_ids,
           "erp_section": "messages", "canned": CANNED_REPLIES}
    if cur:
        thread = []
        for m in ContactMessage.objects.filter(email=cur.email).prefetch_related("attachments", "replies"):
            atts = [{"name": a.original_name or a.file.name.split("/")[-1], "url": a.file.url,
                     "image": (a.content_type or "").startswith("image/"), "pdf": "pdf" in (a.content_type or "")}
                    for a in m.attachments.all() if a.file]
            thread.append({"kind": "in", "at": m.created_at, "m": m, "atts": atts})
            for r in m.replies.all():
                thread.append({"kind": "out", "at": r.at, "r": r})
        thread.sort(key=lambda x: x["at"])
        orders = list(Order.objects.filter(customer_email__iexact=cur.email).order_by("-created_at")[:8])
        last = orders[0] if orders else None
        first = (cur.name or "").split()[0] if cur.name else ""
        ctx.update(thread=thread, orders=orders, paid=sum(o.total for o in orders if o.status in Order.PAID_STATUSES),
                   canvases=DigitalCanvas.objects.filter(email__iexact=cur.email).count(),
                   fill=_json.dumps({"prenom": first, "commande": last.uid if last else "",
                                     "suivi": (" (suivi %s %s%s)" % (last.carrier, last.tracking_number,
                                               " : " + last.tracking_url if last.tracking_url else "")).replace("  ", " ")
                                     if last and last.tracking_number else ""}))
    return render(request, "admin/erp_inbox.html", ctx)


# ---------------- Double authentification des comptes admin ----------------
def _safe_next(request):
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.GET.get("next") or request.POST.get("next") or "/admin-hub/"
    return nxt if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}) else "/admin-hub/"


@staff_member_required
def two_factor(request):
    from .models import StaffTOTP
    dev = StaffTOTP.objects.filter(user=request.user).first()
    if not dev:
        return redirect("studio:two_factor_setup")
    error = ""
    if request.method == "POST":
        if security.rate_limited("otp:%s" % request.user.pk, 5, 300):
            error = "Trop d'essais : patientez 5 minutes."
        elif security.totp_verify(dev.secret, request.POST.get("code")):
            request.session["otp_ok"] = request.user.pk
            return redirect(_safe_next(request))
        else:
            error = "Code incorrect."
    return render(request, "admin/two_factor.html", {"mode": "verify", "error": error, "next": _safe_next(request)})


@staff_member_required
def two_factor_setup(request):
    from .models import StaffTOTP
    if StaffTOTP.objects.filter(user=request.user).exists():
        return redirect("studio:two_factor")
    secret = request.session.get("otp_pending") or security.totp_secret()
    request.session["otp_pending"] = secret
    error = ""
    if request.method == "POST":
        if security.totp_verify(secret, request.POST.get("code")):
            StaffTOTP.objects.create(user=request.user, secret=secret)
            request.session.pop("otp_pending", None)
            request.session["otp_ok"] = request.user.pk
            from django.contrib import messages
            messages.success(request, "Double authentification activée.")
            return redirect("/admin-hub/")
        error = "Code incorrect : vérifiez l'heure de votre téléphone et réessayez."
    uri = security.totp_uri(secret, request.user.get_username())
    import segno
    qr = segno.make(uri, error="m").svg_inline(scale=5, dark="#12224f")
    return render(request, "admin/two_factor.html", {"mode": "setup", "qr": qr, "secret": secret, "error": error})


# ---------------- Remises (ERP) ----------------
@staff_member_required
def erp_discounts(request):
    from django.contrib import messages
    from django.db.models import Count, Q, Sum
    from .models import Discount
    if request.method == "POST":
        op = request.POST.get("op")
        d = Discount.objects.filter(pk=request.POST.get("pk")).first()
        if op == "create":
            import secrets as _sec
            code = discounts.norm(request.POST.get("code")) or ("PROMO-" + _sec.token_hex(3).upper())
            try:
                pct = max(1, min(90, int(request.POST.get("percent") or 10)))
                uses = max(0, int(request.POST.get("max_uses") or 0))
            except ValueError:
                pct, uses = 10, 0
            if Discount.objects.filter(code=code).exists():
                messages.error(request, "Le code %s existe déjà." % code)
            else:
                Discount.objects.create(code=code, kind=Discount.PROMO, percent=pct, max_uses=uses, active=True)
                messages.success(request, "Code %s créé (-%d %%, %s)." % (code, pct, "illimité" if uses == 0 else "%d utilisation(s)" % uses))
        elif d and op == "toggle":
            d.active = not d.active
            d.save(update_fields=["active"])
            messages.success(request, "%s %s." % (d.code, "réactivé" if d.active else "désactivé"))
        elif d and op == "update":
            try:
                d.percent = max(1, min(90, int(request.POST.get("percent") or d.percent)))
                if d.kind == Discount.PROMO:
                    d.max_uses = max(0, int(request.POST.get("max_uses") or 0))
                d.save(update_fields=["percent", "max_uses"])
                messages.success(request, "%s mis à jour." % d.code)
            except ValueError:
                messages.error(request, "Valeurs invalides.")
        elif d and op == "delete":
            if d.used_count or d.status == Discount.USED:
                messages.error(request, "%s a déjà servi : désactivez-le plutôt (historique des commandes)." % d.code)
            else:
                d.delete()
                messages.success(request, "Code supprimé.")
        return redirect(request.get_full_path())

    tab = request.GET.get("tab", "all")
    q = (request.GET.get("q") or "").strip()
    qs = Discount.objects.all().order_by("-created_at")
    tabs_q = {"promo": Q(kind=Discount.PROMO), "referral": Q(kind=Discount.LOYALTY, code__endswith="-R"),
              "loyalty": Q(kind=Discount.LOYALTY) & ~Q(code__endswith="-R"), "all": Q()}
    tabs = [(k, l, qs.filter(tabs_q[k]).count()) for k, l in
            (("promo", "Codes promo"), ("referral", "Parrainage"), ("loyalty", "Fidélité (poster)"), ("all", "Tous"))]
    qs = qs.filter(tabs_q.get(tab, Q()))
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(used_by__icontains=q))
    paid = Order.objects.filter(status__in=Order.PAID_STATUSES).exclude(discount_code__isnull=True).exclude(discount_code="")
    per = {r["discount_code"]: r for r in paid.values("discount_code").annotate(n=Count("id"), ca=Sum("total"), rem=Sum("discount_amount"))}
    rows = []
    for d in qs[:300]:
        st = per.get(d.code, {})
        if not d.active:
            state = ("off", "Désactivé")
        elif discounts.is_redeemable(d.code):
            state = ("ok", "Actif")
        else:
            state = ("used", "Épuisé")
        rows.append({"d": d, "orders": st.get("n", 0), "ca": st.get("ca") or 0, "rem": st.get("rem") or 0, "state": state,
                     "uses": ("%d / ∞" % d.used_count) if (d.kind == Discount.PROMO and d.max_uses == 0) else
                             ("%d / %d" % (d.used_count, d.max_uses) if d.kind == Discount.PROMO else
                              ("1 / 1" if d.status == Discount.USED else "0 / 1"))})
    agg = paid.aggregate(n=Count("id"), ca=Sum("total"), rem=Sum("discount_amount"))
    kpi = {"active": sum(1 for x in Discount.objects.filter(active=True) if discounts.is_redeemable(x.code)),
           "orders": agg["n"] or 0, "ca": agg["ca"] or 0, "rem": agg["rem"] or 0,
           "share": round(100.0 * (agg["n"] or 0) / max(1, Order.objects.filter(status__in=Order.PAID_STATUSES).count()))}
    p = Pricing.get()
    return render(request, "admin/erp_discounts.html", {
        "rows": rows, "tabs": tabs, "tab": tab, "q": q, "kpi": kpi,
        "rates": {"ref": round(p.referral_rate * 100), "phys": round(p.referral_physical_rate * 100)}})


# ---------------- Infos legales (ERP) ----------------
@staff_member_required
def erp_legal(request):
    from django import forms as _f
    from django.contrib import messages
    from .models import CompanyInfo

    class LegalForm(_f.ModelForm):
        class Meta:
            model = CompanyInfo
            exclude = []
            widgets = {"address": _f.Textarea(attrs={"rows": 3}), "host": _f.Textarea(attrs={"rows": 3}),
                       "mediator": _f.Textarea(attrs={"rows": 2})}

    info = CompanyInfo.get()
    form = LegalForm(request.POST or None, instance=info)
    if request.method == "POST" and form.is_valid():
        form.save()
        from django.core.cache import cache
        cache.delete("site_socials")
        cache.delete("site_company")
        messages.success(request, "Informations légales enregistrées : pages publiques et factures à jour.")
        return redirect("studio:erp_legal")
    sections = [
        ("Entreprise", "Apparaît dans les mentions légales, les CGV et sur chaque facture.",
         ["legal_name", "legal_form", "capital", "siret", "rcs", "address", "director"]),
        ("Contact", "Adresse affichée aux clients pour le SAV et les réclamations.", ["email", "phone"]),
        ("TVA et factures", "Taux 0 = micro-entreprise en franchise (mention art. 293 B du CGI sur la facture).",
         ["vat_rate", "vat_number", "invoice_prefix"]),
        ("Banque", "Compte débité pour les virements fournisseurs (ordres SEPA générés par l'ERP).", ["bank_iban", "bank_bic"]),
        ("Fabrication", "Zone de fabrication réelle : utilisée uniquement pour la politique de confidentialité (transferts de données). Le site affiche « Fabriqué à la demande par nos ateliers partenaires ».",
         ["production_origin"]),
        ("Hébergeur et médiation", "Obligatoires : nom, adresse et téléphone de l'hébergeur, médiateur de la consommation.",
         ["host", "mediator"]),
        ("Réseaux sociaux", "Liens affichés dans le pied de page du site et déclarés à Google (sameAs) : ils relient le site à vos comptes officiels.",
         ["tiktok_url", "instagram_url", "facebook_url", "pinterest_url", "youtube_url"]),
    ]
    checks = [("Raison sociale", bool(info.legal_name)), ("Adresse du siège", bool(info.address)),
              ("SIRET", bool(info.siret)), ("Hébergeur", bool(info.host)),
              ("Médiateur de la consommation", bool(info.mediator)), ("E-mail de contact", bool(info.email))]
    return render(request, "admin/erp_legal.html", {
        "form": form, "checks": checks, "done": sum(1 for _, ok in checks if ok),
        "sections": [{"title": t, "help": h, "fields": [form[n] for n in names]} for t, h, names in sections]})


# ---------------- Communautes (compte de marque assume) ----------------
def go_link(request, code):
    """Lien traque d'une publication communaute : compte le clic, attribue la vente, redirige."""
    from django.db.models import F
    from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse
    from .models import Outreach
    o = Outreach.objects.filter(code=code).select_related("community").first()
    if not o:
        return redirect("studio:home")
    who = _visitor_kind(request)
    if who == "human":
        upd = {"clicks": F("clicks") + 1, "last_click_at": timezone.now()}
        k = "go_%s" % o.pk
        if not request.session.get(k):
            request.session[k] = 1
            upd["unique_clicks"] = F("unique_clicks") + 1
        Outreach.objects.filter(pk=o.pk).update(**upd)
    if who != "bot":
        request.session["ad_ref"] = ("c:" + o.code)[:24]       # attribution de la vente (dernier clic)
    u = urlparse(o.target_url or settings.SITE_URL + "/create/")
    q = dict(parse_qsl(u.query))
    q.update({"utm_source": (o.community.platform if o.community else "community"),
              "utm_medium": "community", "utm_campaign": (o.community.name if o.community else "outreach")[:60],
              "utm_content": o.code})
    resp = redirect(urlunparse(u._replace(query=urlencode(q))))
    resp["Cache-Control"] = "no-store"
    return resp


@staff_member_required
def erp_communities(request):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from django.db.models import Count, Sum
    from .models import Community, Outreach
    from . import community as cm
    if request.method == "POST":
        op = request.POST.get("op")
        try:
            if op == "seed":
                n, d = cm.seed()
                messages.success(request, "Audit chargé : %d nouvelle(s) communauté(s), %d brouillon(s) de publication." % (n, d))
            elif op == "add_comm":
                Community.objects.create(platform=request.POST.get("platform", "reddit"),
                                         name=request.POST.get("name", "").strip()[:120] or "Communauté",
                                         url=request.POST.get("url", "").strip()[:300],
                                         region=request.POST.get("region", "global"),
                                         lang=request.POST.get("lang", "en")[:5],
                                         promo=request.POST.get("promo", "unknown"),
                                         angle=request.POST.get("angle", "").strip())
                messages.success(request, "Communauté ajoutée.")
            elif op == "edit_comm":
                c = Community.objects.get(pk=request.POST.get("id"))
                for f in ("name", "url", "region", "lang", "promo", "angle", "notes", "rules"):
                    if f in request.POST:
                        setattr(c, f, request.POST.get(f, "").strip())
                if (request.POST.get("priority") or "").isdigit():
                    c.priority = int(request.POST["priority"])
                c.active = bool(request.POST.get("active"))
                c.save()
                messages.success(request, "Communauté mise à jour.")
            elif op == "del_comm":
                Community.objects.filter(pk=request.POST.get("id")).delete()
            elif op == "refresh_reddit":
                n = 0
                for c in Community.objects.filter(platform="reddit", active=True):
                    try:
                        n += cm.refresh_subreddit(c)
                    except Exception as exc:
                        messages.error(request, "%s : %s" % (c.name, exc))
                    time.sleep(1)
                messages.success(request, "%d subreddit(s) actualisé(s) (membres + règles)." % n)
            elif op == "new_post":
                c = Community.objects.filter(pk=request.POST.get("community")).first()
                o = Outreach.objects.create(community=c, code=cm.new_code(),
                                            title=request.POST.get("title", "").strip()[:300],
                                            body=request.POST.get("body", "").strip(),
                                            target_url=request.POST.get("target", "").strip()[:300]
                                            or settings.SITE_URL + "/create/")
                messages.success(request, "Publication préparée : lien traqué %s" % cm.tracked_url(o))
            elif op == "mark_posted":
                o = Outreach.objects.get(pk=request.POST.get("id"))
                o.posted_url = request.POST.get("posted_url", "").strip()[:400]
                o.posted_at = o.posted_at or timezone.now()
                o.save(update_fields=["posted_url", "posted_at"])
                if "reddit.com" in o.posted_url:
                    cm.refresh_post(o)
                messages.success(request, "Publication enregistrée : suivi activé.")
            elif op == "seen":
                o = Outreach.objects.get(pk=request.POST.get("id"))
                o.comments_seen = o.comments or 0
                o.save(update_fields=["comments_seen"])
            elif op == "refresh_posts":
                n = 0
                for o in Outreach.objects.exclude(posted_url="").filter(posted_url__contains="reddit.com"):
                    try:
                        n += cm.refresh_post(o)
                    except Exception as exc:
                        messages.error(request, "%s : %s" % (o.code, exc))
                messages.success(request, "%d publication(s) actualisée(s)." % n)
            elif op == "del_post":
                Outreach.objects.filter(pk=request.POST.get("id")).delete()
        except Exception as exc:
            logger.exception("Communautes")
            messages.error(request, "Erreur : %s" % exc)
        return redirect(request.get_full_path())
    region = request.GET.get("region", "")
    comms = Community.objects.all()
    if region:
        comms = comms.filter(region=region)
    conv = {r["ad_ref"]: r for r in Order.objects.filter(status__in=Order.PAID_STATUSES, ad_ref__startswith="c:")
            .values("ad_ref").annotate(n=Count("id"), ca=Sum("total"))}
    posts = list(Outreach.objects.select_related("community")[:200])
    # brouillons d'abord, par priorite du canal ; puis publications, les plus recentes en premier
    posts.sort(key=lambda o: (bool(o.posted_url), (o.community.priority if o.community else 9) if not o.posted_url else 0,
                              -(o.posted_at.timestamp() if o.posted_at else 0)))
    for o in posts:
        r = conv.get("c:" + o.code) or {}
        o.orders, o.revenue = r.get("n", 0), round(r.get("ca") or 0, 2)
        o.link = cm.tracked_url(o)
        o.submit = cm.submit_url(o.community, o.title, o.body, o.link) if not o.posted_url else ""
    tot = {"clicks": sum(o.unique_clicks for o in posts), "orders": sum(o.orders for o in posts),
           "revenue": round(sum(o.revenue for o in posts), 2), "replies": sum(o.new_replies for o in posts)}
    return render(request, "admin/erp_communities.html", {
        **_admin.site.each_context(request), "comms": comms, "posts": posts, "tot": tot, "region": region,
        "regions": Community.REGIONS, "platforms": Community.PLATFORMS, "promos": Community.PROMO,
        "reddit_oauth": bool(settings.REDDIT_CLIENT_ID), "erp_section": "growth", "gtab": "communities"})


@staff_member_required
def erp_community_library(request):
    """Bibliotheque de publications : visualiser / modifier les modeles, les envoyer vers une ou plusieurs communautes
    (un lien traque unique par communaute)."""
    from django.contrib import admin as _admin
    from django.contrib import messages
    from django.db.models import Count, Sum
    from .models import Community, Outreach, PostTemplate
    from . import community as cm
    from .community_library import seed_library
    if request.method == "POST":
        op = request.POST.get("op")
        t = PostTemplate.objects.filter(pk=request.POST.get("id")).first()
        try:
            if op == "seed":
                from . import library_visuals
                messages.success(request, "%d modèle(s) ajouté(s). Leurs visuels se génèrent en arrière-plan." % seed_library())
                library_visuals.build_missing_async()
            elif op == "visuals":
                from . import library_visuals
                if not library_visuals.base_canvases():
                    messages.error(request, "Aucune toile disponible : ajoutez d'abord des modèles au catalogue.")
                else:
                    library_visuals.build_missing_async()
                    messages.success(request, "Génération des visuels lancée. Rechargez la page dans une minute.")
            elif op == "visual" and t:
                from . import library_visuals
                ad = library_visuals.build(t)
                (messages.success if ad else messages.error)(
                    request, "Visuel généré." if ad else "Aucune toile disponible pour générer le visuel.")
            elif op in ("save", "new"):
                t = t if op == "save" and t else PostTemplate()
                for f in ("name", "category", "lang", "platforms", "hook", "body", "media_kind", "media_brief", "target_path"):
                    if f in request.POST:
                        setattr(t, f, request.POST.get(f, "").strip())
                t.name = t.name or t.hook[:60] or "Nouveau modèle"
                t.active = bool(request.POST.get("active", "on" if op == "new" else ""))
                if request.FILES.get("media"):
                    t.media = request.FILES["media"]
                if request.POST.get("media_clear"):
                    t.media = None
                t.save()
                messages.success(request, "Modèle « %s » enregistré." % t.name)
            elif op == "dup" and t:
                t.pk = None; t.name = (t.name + " (copie)")[:120]; t.save()
                messages.success(request, "Modèle dupliqué.")
            elif op == "del" and t:
                t.delete()
            elif op == "use" and t:
                ids = [int(x) for x in request.POST.getlist("communities") if x.isdigit()]
                made = []
                for c in Community.objects.filter(pk__in=ids):
                    o = Outreach.objects.create(community=c, template=t, code=cm.new_code(), title=t.hook,
                                                body=t.body, target_url=settings.SITE_URL + (t.target_path or "/create/"))
                    link = cm.tracked_url(o)
                    if "{link}" in o.body:
                        o.body = o.body.replace("{link}", link); o.save(update_fields=["body"])
                    made.append(c.name)
                if made:
                    messages.success(request, "« %s » préparé pour %d communauté(s) : %s. Retrouvez-les dans Publications "
                                     "(un lien traqué par communauté)." % (t.name, len(made), ", ".join(made)))
                else:
                    messages.error(request, "Choisissez au moins une communauté.")
        except Exception as exc:
            logger.exception("Bibliotheque")
            messages.error(request, "Erreur : %s" % exc)
        return redirect(request.get_full_path())
    cat, lang, plat = request.GET.get("cat", ""), request.GET.get("lang", ""), request.GET.get("plat", "")
    qs = PostTemplate.objects.annotate(uses=Count("posts"), uclicks=Sum("posts__unique_clicks"))
    if cat:
        qs = qs.filter(category=cat)
    if lang:
        qs = qs.filter(lang=lang)
    if plat:
        qs = qs.filter(platforms__contains=plat)
    conv = {}
    for r in Order.objects.filter(status__in=Order.PAID_STATUSES, ad_ref__startswith="c:").values("ad_ref").annotate(n=Count("id")):
        conv[r["ad_ref"][2:]] = r["n"]
    tpls = list(qs)
    codes = {}
    for o in Outreach.objects.exclude(template=None).values("template_id", "code"):
        codes.setdefault(o["template_id"], []).append(o["code"])
    from .models import MarketingAd
    from . import library_visuals
    ads = {}
    for a in MarketingAd.objects.filter(image_uid__in=[t.ad_uid for t in tpls if t.ad_uid]):
        ads.setdefault((a.image_uid, a.variant), []).append(a)
    missing = 0
    for t in tpls:
        t.orders = sum(conv.get(c, 0) for c in codes.get(t.pk, []))
        group = ads.get((t.ad_uid, t.ad_variant), [])
        want = t.visual_kind()
        t.vis = next((a for a in group if a.kind == want), None) or next((a for a in group if a.fmt == "gif"), None)
        missing += t.media_kind != "text" and not t.vis
    if missing and library_visuals.base_canvases():
        library_visuals.build_missing_async()      # visuels prets d'office : generes en arriere-plan
    comms = list(Community.objects.filter(active=True))
    return render(request, "admin/erp_community_library.html", {"missing_visuals": missing,
        **_admin.site.each_context(request), "tpls": tpls, "comms": comms, "cat": cat, "lang": lang, "plat": plat,
        "categories": PostTemplate.CATEGORIES, "medias": PostTemplate.MEDIA,
        "langs": sorted(set(PostTemplate.objects.values_list("lang", flat=True))),
        "plats": [("reddit", "Reddit"), ("facebook", "Facebook"), ("social", "Réseaux sociaux"), ("launch", "Lancement"),
                  ("forum", "Forums / Q&A"), ("discord", "Discord")],
        "erp_section": "growth", "gtab": "library"})


def review_page(request, uid):
    """Depot d'un avis client verifie (lien personnel envoye apres livraison)."""
    import hmac as _hmac
    from .models import Review
    o = Order.objects.filter(uid=uid).first()
    tok = request.GET.get("t") or request.POST.get("t") or ""
    if not o or not _hmac.compare_digest(tok, security.review_token(uid)):
        raise Http404
    if o.status not in (Order.SHIPPED, Order.DELIVERED) + Order.AT_SUPPLIER:
        raise Http404
    try:
        r = o.review
    except Review.DoesNotExist:
        r = None
    parts = (o.customer_name or "").split()
    default_name = (parts[0] + (" %s." % parts[-1][0] if len(parts) > 1 else "")) if parts else _("Client vérifié")
    sent = False
    if request.method == "POST":
        try:
            rating = max(1, min(5, int(request.POST.get("rating", "0"))))
        except ValueError:
            rating = 0
        if rating:
            text = (request.POST.get("text") or "").strip()[:2000]
            name = (request.POST.get("author") or default_name).strip()[:60] or default_name
            if r:
                r.rating, r.text, r.author = rating, text, name
                r.save(update_fields=["rating", "text", "author"])
            else:
                r = Review.objects.create(order=o, rating=rating, text=text, author=name, lang=get_language() or "fr")
            sent = True
    return render(request, "studio/review.html", {"o": o, "r": r, "t": tok, "sent": sent, "default_name": default_name,
                                                  "stars": [1, 2, 3, 4, 5]})


@staff_member_required
def erp_reviews(request):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from django.db.models import Avg, Count
    from .models import Review
    if request.method == "POST":
        r = Review.objects.filter(pk=request.POST.get("id")).first()
        if r and request.POST.get("op") == "hide":
            r.published, r.hidden_reason = False, (request.POST.get("reason") or "Contenu abusif / hors sujet")[:120]
            r.save(update_fields=["published", "hidden_reason"])
        elif r and request.POST.get("op") == "show":
            r.published, r.hidden_reason = True, ""
            r.save(update_fields=["published", "hidden_reason"])
        messages.success(request, "Avis mis à jour.")
        return redirect(request.get_full_path())
    agg = Review.objects.filter(published=True).aggregate(avg=Avg("rating"), n=Count("id"))
    return render(request, "admin/erp_reviews.html", {
        **_admin.site.each_context(request), "reviews": Review.objects.select_related("order")[:300], "agg": agg,
        "erp_section": "reviews"})


# ---------------- Croissance : campagnes (ciblage, contenus, diffusion, retombees) ----------------
def _campaign_targeting(c, P):
    for f in ("regions", "langs", "platforms", "categories"):
        setattr(c, f, ",".join(P.getlist(f)))
    if (P.get("min_priority") or "").isdigit():
        c.min_priority = int(P["min_priority"])
    c.exclude_forbidden = bool(P.get("exclude_forbidden"))
    return c


def _campaign_settings(c, P):
    from .models import Discount
    c.name = (P.get("name") or c.name or "Campagne").strip()[:80]
    c.goal = P.get("goal", "").strip()
    c.status = P.get("status") or c.status
    for f in ("start", "end"):
        setattr(c, f, (P.get(f) or "").strip() or None)
    try:
        c.budget = float((P.get("budget") or "0").replace(",", "."))
    except ValueError:
        c.budget = 0
    c.append_code = bool(P.get("append_code"))
    c.target_path = (P.get("target_path") or "/create/").strip()[:120]
    did = P.get("discount") or ""
    c.discount = Discount.objects.filter(pk=did).first() if did.isdigit() else None
    return c


@staff_member_required
def growth_hub(request):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from .models import Campaign
    from . import growth
    if request.method == "POST" and request.POST.get("op") == "create":
        preset = request.POST.get("preset", "")
        pname = next((p["name"] for p in growth.PRESETS if p["key"] == preset), "")
        name = (request.POST.get("name") or "").strip()[:80] or pname or "Nouvelle campagne"
        base, n = name, 2
        while Campaign.objects.filter(name=name).exists():
            name = "%s (%d)" % (base, n); n += 1
        c = growth.apply_preset(Campaign(name=name, goal=request.POST.get("goal", "").strip()), preset)
        c.save()
        if preset and request.POST.get("generate"):
            made, _sk = growth.generate_actions(c)
            if made:
                c.status = "active"; c.save(update_fields=["status"])
            messages.success(request, "Campagne « %s » créée : %d action(s) ciblée(s) prête(s) à publier." % (c.name, len(made)))
        else:
            messages.success(request, "Campagne « %s » créée. Ajustez le ciblage puis générez les actions." % c.name)
        return redirect("studio:growth_campaign", pk=c.pk)
    camps = list(Campaign.objects.all())
    totals = {"clicks": 0, "orders": 0, "revenue": 0.0, "budget": 0.0}
    for c in camps:
        c.s = growth.stats(c)["tot"]
        totals["clicks"] += c.s["clicks"]; totals["orders"] += c.s["orders"]
        totals["revenue"] += c.s["revenue"]; totals["budget"] += c.budget
    totals["revenue"] = round(totals["revenue"], 2)
    for c in camps:
        c.pct = round(c.s["posted"] / c.s["actions"] * 100) if c.s["actions"] else 0
    return render(request, "admin/growth_hub.html", {**_admin.site.each_context(request), "camps": camps,
                                                     "totals": totals, "presets": growth.PRESETS,
                                                     "erp_section": "growth", "gtab": "campaigns"})


@staff_member_required
def growth_match(request, pk):
    """Apercu en direct du ciblage (sans enregistrer) : nombre et liste des communautes visees."""
    from .models import Campaign
    from . import growth
    c = Campaign.objects.filter(pk=pk).first() or Campaign()
    for f in ("regions", "langs", "platforms", "categories"):
        setattr(c, f, ",".join(request.GET.getlist(f)))
    if (request.GET.get("min_priority") or "").isdigit():
        c.min_priority = int(request.GET["min_priority"])
    c.exclude_forbidden = request.GET.get("exclude_forbidden") == "1"
    m = growth.match_communities(c)
    return JsonResponse({"count": len(m), "items": [{"name": x.name, "platform": x.get_platform_display(),
                                                     "lang": x.lang} for x in m[:40]]})


@staff_member_required
def growth_campaign(request, pk):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from .models import Campaign, Community, Discount, Outreach, PostTemplate
    from . import growth, community as cm
    c = Campaign.objects.filter(pk=pk).first()
    if not c:
        raise Http404
    if request.method == "POST":
        op = request.POST.get("op")
        try:
            if op in ("target", "target_generate"):
                _campaign_targeting(c, request.POST).save()
                if op == "target":
                    messages.success(request, "Ciblage enregistré.")
            if op == "settings":
                _campaign_settings(c, request.POST).save()
                messages.success(request, "Réglages enregistrés.")
            if op in ("generate", "target_generate"):
                made, skipped = growth.generate_actions(c)
                if c.status == "draft":
                    c.status = "active"; c.save(update_fields=["status"])
                if made:
                    messages.success(request, "%d publication(s) personnalisée(s) prête(s) à diffuser." % len(made))
                else:
                    messages.warning(request, "Aucune nouvelle publication : %s" % (
                        "; ".join(skipped[:6]) if skipped else "aucune communauté ne correspond au ciblage."))
            if op == "new_code":
                import secrets as _s
                d = Discount.objects.create(code=("%s-%s" % (cm._slug_code(c.name), _s.token_hex(2))).upper()[:32],
                                            kind=Discount.PROMO, percent=int(request.POST.get("percent") or 10), max_uses=0)
                c.discount = d; c.save(update_fields=["discount"])
                messages.success(request, "Code promo %s (-%d %%) créé et rattaché à la campagne." % (d.code, d.percent))
            elif op == "mark_posted":
                o = Outreach.objects.get(pk=request.POST.get("id"), campaign=c)
                o.posted_url = request.POST.get("posted_url", "").strip()[:400]
                o.posted_at = o.posted_at or timezone.now()
                o.save(update_fields=["posted_url", "posted_at"])
                if "reddit.com" in o.posted_url:
                    cm.refresh_post(o)
            elif op == "seen":
                o = Outreach.objects.get(pk=request.POST.get("id"), campaign=c)
                o.comments_seen = o.comments or 0; o.save(update_fields=["comments_seen"])
            elif op == "del_post":
                Outreach.objects.filter(pk=request.POST.get("id"), campaign=c).delete()
            elif op == "refresh":
                n = 0
                for o in Outreach.objects.filter(campaign=c).exclude(posted_url="").filter(posted_url__contains="reddit.com"):
                    n += cm.refresh_post(o)
                messages.success(request, "%d publication(s) actualisée(s)." % n)
            elif op == "delete":
                c.delete()
                messages.success(request, "Campagne supprimée (les publications restent dans le suivi).")
                return redirect("studio:growth_hub")
        except Exception as exc:
            logger.exception("Campagne")
            messages.error(request, "Erreur : %s" % exc)
        return redirect(request.get_full_path())
    st = growth.stats(c)
    for o in st["posts"]:
        o.link = cm.tracked_url(o)
        o.submit = cm.submit_url(o.community, o.title, o.body, o.link) if not o.posted_url else ""
    matched = growth.match_communities(c)
    t = st["tot"]
    steps = [
        {"k": 1, "id": "ciblage", "t": "Cibler", "s": "%d communauté%s" % (len(matched), "s" if len(matched) > 1 else ""),
         "done": bool(matched)},
        {"k": 2, "id": "actions", "t": "Générer", "s": "%d action%s prête%s" % (t["actions"], "s" * (t["actions"] > 1), "s" * (t["actions"] > 1)),
         "done": t["actions"] > 0},
        {"k": 3, "id": "actions", "t": "Publier", "s": "%d / %d publiée%s" % (t["posted"], t["actions"], "s" * (t["posted"] > 1)),
         "done": t["actions"] > 0 and t["posted"] == t["actions"]},
        {"k": 4, "id": "resultats", "t": "Mesurer", "s": "%d visiteur%s · %d vente%s" % (t["clicks"], "s" * (t["clicks"] > 1), t["orders"], "s" * (t["orders"] > 1)),
         "done": t["orders"] > 0},
    ]
    cur = next((x["k"] for x in steps if not x["done"]), 4)
    funnel_raw = [("Actions préparées", t["actions"]), ("Publiées", t["posted"]), ("Visiteurs", t["clicks"]), ("Ventes", t["orders"])]
    top = max([v for _l, v in funnel_raw] + [1])
    funnel = [{"l": l, "v": v, "w": round(v / top * 100, 1)} for l, v in funnel_raw]
    chmax = max([ch["clicks"] for ch in st["channels"].values()] + [1])
    channels = [{"l": k, "w": round(ch["clicks"] / chmax * 100, 1), **ch} for k, ch in st["channels"].items()]
    todo = sum(1 for o in st["posts"] if not o.posted_url)
    return render(request, "admin/growth_campaign.html", {
        "steps": steps, "cur": cur, "funnel": funnel, "channels": channels, "todo": todo,
        **_admin.site.each_context(request), "c": c, "st": st, "matched": matched,
        "regions": Community.REGIONS, "platforms": Community.PLATFORMS, "categories": PostTemplate.CATEGORIES,
        "langs": sorted(set(Community.objects.values_list("lang", flat=True)) | {"en", "fr", "de", "es", "it"}),
        "discounts": Discount.objects.filter(active=True, kind=Discount.PROMO).order_by("-created_at")[:50],
        "statuses": Campaign.STATUS, "erp_section": "growth", "gtab": "campaigns",
        "prio_opts": [(1, "Priorité haute uniquement"), (2, "Priorités haute et moyenne"), (3, "Toutes les priorités")],
        "paths": [("/create/", "Création (aperçu gratuit)"), ("/play/", "Jouer (jeu gratuit)"), ("/", "Accueil")]})


# ---------------------------------------------------------------- Comptabilite
@staff_member_required
@_test_scope
def erp_compta(request):
    """Comptabilite mensuelle : ventes, TVA, remboursements, frais, achats, marge ; rapprochement ; exports."""
    from django.contrib import admin as _admin
    from django.contrib import messages
    from . import ledger
    from .models import CompanyInfo, SupplierPayment
    if request.method == "POST" and request.POST.get("op") == "sync":
        n = ledger.sync_orders()
        try:
            r = ledger.sync_stripe(days=int(request.POST.get("days") or 40))
        except Exception as exc:
            logger.exception("Rapprochement Stripe")
            r = {"error": str(exc)[:200]}
        if r.get("error"):
            messages.error(request, "Rapprochement Stripe impossible : %s" % r["error"])
        elif r.get("skipped"):
            messages.success(request, "%d vente(s) rattrapée(s) depuis les commandes. %s." % (n, r["skipped"]))
        else:
            messages.success(request, "Rapprochement terminé : %d vente(s), %d frais, %d remboursement(s), %d virement(s) "
                             "Stripe ; %d commande(s) rattrapée(s)." % (r["sales"], r["fees"], r["refunds"], r["payouts"], n))
        return redirect(request.get_full_path())
    ledger.sync_orders()        # aucune commande payee hors grand livre
    start, a, b = ledger.month_bounds(request.GET.get("m"))
    prev = (start - datetime.timedelta(days=1)).strftime("%Y-%m")
    nxt = (start + datetime.timedelta(days=32)).replace(day=1)
    months = []
    d = timezone.localdate().replace(day=1)
    for _i in range(12):
        months.append((d.strftime("%Y-%m"), d))
        d = (d - datetime.timedelta(days=1)).replace(day=1)
    rows = list(ledger.entries(a, b).order_by("-date")[:400])
    return render(request, "admin/erp_compta.html", {
        **_admin.site.each_context(request), "erp_section": "compta", "m": start.strftime("%Y-%m"), "month": start, "testdata": _test_info(request),
        "prev": prev, "next": nxt.strftime("%Y-%m") if nxt <= timezone.localdate() else "", "months": months,
        "s": ledger.summary(a, b), "rows": rows, "oss": ledger.oss_status(start.year),
        "vat_rate": ledger.vat_rate(), "company": CompanyInfo.get(), "stripe_on": bool(settings.STRIPE_SECRET_KEY),
        "drafts": SupplierPayment.objects.filter(status="draft").select_related("supplier")})


@staff_member_required
def erp_compta_export(request, month, kind):
    from . import ledger
    start, a, b = ledger.month_bounds(month)
    tag = start.strftime("%Y-%m")
    if kind == "ventes.csv":
        resp = HttpResponse(ledger.sales_csv(a, b), content_type="text/csv; charset=utf-8")
    elif kind == "achats.csv":
        resp = HttpResponse(ledger.purchases_csv(a, b), content_type="text/csv; charset=utf-8")
    elif kind == "ecritures.txt":
        resp = HttpResponse(ledger.fec_lines(a, b), content_type="text/plain; charset=utf-8")
    elif kind == "factures.zip":
        resp = HttpResponse(ledger.invoices_zip(a, b), content_type="application/zip")
    else:
        raise Http404
    resp["Content-Disposition"] = 'attachment; filename="paintit_%s_%s"' % (tag, kind)
    return resp


@staff_member_required
def erp_supplier_payment(request, pk):
    """Reglement fournisseur : instructions de virement (IBAN, montants, reference), SEPA XML, confirmation."""
    from django.contrib import admin as _admin
    from django.contrib import messages
    from . import ledger
    from .models import SupplierPayment
    pay = SupplierPayment.objects.select_related("supplier").filter(pk=pk).first()
    if not pay:
        raise Http404
    if request.GET.get("sepa") and ledger.sepa_eligible(pay.supplier):
        resp = HttpResponse(ledger.sepa_xml(pay), content_type="application/xml")
        resp["Content-Disposition"] = 'attachment; filename="%s.xml"' % pay.reference
        return resp
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "confirm":
            try:
                day = datetime.date.fromisoformat(request.POST.get("paid_at") or "")
            except ValueError:
                day = timezone.localdate()
            ledger.confirm_payment(pay, day, request.POST.get("bank_ref", ""))
            messages.success(request, "Virement confirmé : %d commande(s) réglée(s), écriture passée en comptabilité."
                             % pay.orders.count())
        elif op == "cancel" and ledger.cancel_payment(pay):
            messages.success(request, "Règlement annulé : les commandes redeviennent à régler.")
            return redirect(reverse("studio:erp_supplier", args=[pay.supplier_id]) + "#commandes")
        return redirect(request.path)
    from .models import CompanyInfo
    return render(request, "admin/erp_supplier_payment.html", {
        **_admin.site.each_context(request), "erp_section": "suppliers", "pay": pay, "sup": pay.supplier,
        "orders": list(pay.orders.order_by("created_at")), "sepa": ledger.sepa_eligible(pay.supplier),
        "company": CompanyInfo.get()})


# ---------------- Croissance : videos « satisfying » TikTok ----------------
HOOKS = [   # (groupe, [accroches]) ; {zones} et {colors} = chiffres reels de la toile
    ("Questions", [
        "How long would it take you to paint {zones} zones with {colors} colors?",
        "Could you paint {zones} zones without giving up?",
        "Guess the picture before the last zone",
        "Which color would you start with?",
        "How many hours would this take you?",
        "Would you have the patience for {colors} colors?",
        "Can you guess what it is yet?",
        "Rate this from 1 to 10 when it's done",
    ]),
    ("Patience & calme", [
        "It takes patience",
        "Patience, one zone at a time",
        "Slow down. Breathe. Paint.",
        "One zone at a time",
        "Your daily dose of calm",
        "Therapy, but with paint",
        "Phone down, brush up",
        "The most relaxing minute of your day",
        "No talent needed, just patience",
        "Small zones, big calm",
    ]),
    ("Satisfying", [
        "Wait for the last zone",
        "Wait for the end",
        "Watch it come to life",
        "{zones} zones. {colors} colors. 1 minute.",
        "This is oddly satisfying",
        "Don't scroll before the last zone",
        "From blank to beautiful",
        "Every zone counts",
        "The last zone hits different",
        "POV: you finally finish it",
    ]),
    ("Photo -> kit", [
        "Your photo -> paint by numbers",
        "Turn any photo into a paint by numbers",
        "This was a photo",
        "Imagine this with your own photo",
        "Your pet, your trip, your family: in {colors} colors",
        "Paint your own memories",
        "A gift idea that takes patience",
        "Send this to someone who needs a new hobby",
    ]),
    ("Français", [
        "Combien de temps pour peindre {zones} zones avec {colors} couleurs ?",
        "Il faut de la patience",
        "Une zone à la fois",
        "Attends la fin",
        "Attends la dernière zone",
        "Ta photo -> peinture par numéros",
        "Ta dose de calme du jour",
        "Pose ton téléphone, prends un pinceau",
        "Tu tiendrais jusqu'à la dernière zone ?",
        "C'était une simple photo",
    ]),
]


@staff_member_required
def erp_videos(request):
    """Videos TikTok / Reels / Shorts : une toile du catalogue se remplit (rendu serveur, musique generee)."""
    import random
    from django.contrib import admin as _admin
    from django.contrib import messages
    from . import satisfying, shorts
    from .models import SocialVideo
    srcs = satisfying.sources()
    titles = dict(srcs)
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "create":
            import uuid as _uuid
            from .forms import DENSITY_LEVELS
            from .pipeline import generate, source_file
            uid = request.POST.get("canvas", "")
            f = request.FILES.get("image")
            fmt = request.POST.get("fmt") if request.POST.get("fmt") in ("fill", "guess", "versus", "tips") else "fill"
            level = request.POST.get("density", "orig")
            try:
                want = int(request.POST.get("colors") or 0)
            except ValueError:
                want = 0
            recolor = uid in titles and 6 <= want <= 64 and want != len(satisfying._palette(uid))
            tmp = os.path.join(settings.MEDIA_ROOT, "uploads", "vid-%s_in.jpg" % _uuid.uuid4().hex[:10])

            def make(src, name, ncol, lvl, label):
                """Toile (re)generee depuis la photo -> uid vid-*."""
                new = "vid-" + _uuid.uuid4().hex[:10]
                dens, det = DENSITY_LEVELS.get(lvl, DENSITY_LEVELS["auto"])
                generate(src, ncol, 40, 50, uid=new, source_name=name, density=dens, detail=det)
                titles[new] = label
                satisfying.save_label(new, label)
                return new
            uid2 = ""
            try:
                if f or (uid in titles and (level != "orig" or recolor or fmt == "versus")):
                    # import depuis l'ordinateur, nouvelle densite ou versus : toile(s) regeneree(s) depuis la photo
                    if f:
                        os.makedirs(os.path.dirname(tmp), exist_ok=True)
                        with open(tmp, "wb") as out:
                            for chunk in f.chunks():
                                out.write(chunk)
                        src, name, label = tmp, f.name, "Image importée"
                    else:
                        src = source_file(os.path.join(settings.MEDIA_ROOT, "orders", uid), uid)
                        if not src:
                            raise ValueError("photo d'origine introuvable pour cette toile")
                        name, label = os.path.basename(src), titles[uid]
                    if fmt == "versus":                   # meme photo : facile 12 couleurs contre detaillee
                        hi = want if 13 <= want <= 64 else 48
                        uid = make(src, name, 12, "easy", label + " · 12 couleurs")
                        uid2 = make(src, name, hi, "expert", label + " · %d couleurs" % hi)
                    else:
                        ncol = want if 6 <= want <= 64 else (24 if f else len(satisfying._palette(uid)))
                        uid = make(src, name, ncol, level, " · ".join(
                            [label] + ([dict(satisfying.DENSITIES)[level]] if level != "orig" else [])
                            + (["%d couleurs" % ncol] if recolor or f else [])))
            except Exception as exc:
                logger.exception("Video canvas")
                messages.error(request, "Toile impossible à créer : %s" % exc)
                return redirect(request.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            if uid not in titles:                          # liste blanche : imports, catalogue, studio
                messages.error(request, "Choisissez une toile du catalogue.")
                return redirect(request.path)
            try:
                dur = float(request.POST.get("duration") or 0)
            except ValueError:
                dur = 0
            n = max(1, min(3, int(request.POST.get("count") or 1)))
            style = request.POST.get("music_style", "ambient")
            style = style if style in dict(satisfying.MUSIC_STYLES) else "ambient"
            series = request.POST.get("series", "").strip()[:20]
            for i in range(n):
                if fmt == "fill":                          # court par defaut : le decrochage tue la portee
                    d = min(70.0, max(15.0, dur)) if dur else round(random.uniform(22, 32), 1)
                else:
                    d = min(30.0, max(10.0, dur)) if dur else shorts.DEFAULT_DURATION[fmt]
                v = SocialVideo.objects.create(
                    canvas_uid=uid, canvas_uid2=uid2, fmt=fmt, title=titles[uid][:120],
                    series=series,
                    end_title=request.POST.get("answer", "").strip()[:120],
                    mode=request.POST.get("mode") if request.POST.get("mode") in ("flow", "random", "colors") else "flow",
                    duration=d, hook=request.POST.get("hook", "")[:120], music=style != "none", music_style=style, before_after=bool(request.POST.get("before_after")),
                    fill_sounds=bool(request.POST.get("fill_sounds")),
                    show_numbers=bool(request.POST.get("show_numbers")),
                    seed=random.randint(1, 10 ** 6))
                satisfying.enqueue(v)
            messages.success(request, "%d vidéo(s) en cours de génération (environ 2 à 3 min chacune)." % n)
        elif op == "delete":
            v = SocialVideo.objects.filter(pk=request.POST.get("id")).first()
            if v and v.status in ("done", "error"):
                for ext in ("mp4", "jpg"):
                    if os.path.exists(v.path(ext)):
                        os.remove(v.path(ext))
                v.delete()
        return redirect(request.path)
    satisfying.mark_stale()
    videos = list(SocialVideo.objects.all()[:60])
    if request.GET.get("format") == "json":
        return JsonResponse({"videos": [{"id": v.id, "status": v.status, "progress": v.progress} for v in videos]})
    caption = "%s\n\n%s" % (satisfying.CAPTIONS["en"], satisfying.HASHTAGS)
    return render(request, "admin/erp_videos.html", {
        **_admin.site.each_context(request), "videos": videos, "sources": srcs, "hooks": HOOKS,
        "picked": request.GET.get("canvas") if request.GET.get("canvas") in titles else (srcs[0][0] if srcs else ""),
        "music_styles": satisfying.MUSIC_STYLES, "densities": satisfying.DENSITIES,
        "titles": {"fill": satisfying.TITLES["en"] + satisfying.TITLES["fr"], **shorts.TITLES},
        "formats": shorts.FORMATS, "hashtags": satisfying.HASHTAGS,
        "caption": caption, "caption_en": "%s\n\n%s" % (satisfying.CAPTIONS["fr"], satisfying.HASHTAGS),
        "pending": any(v.status in ("queued", "running") for v in videos),
        "erp_section": "growth", "gtab": "videos"})


@staff_member_required
def erp_video_file(request, pk, ext):
    from .models import SocialVideo
    v = SocialVideo.objects.filter(pk=pk, status="done").first()
    if not v or ext not in ("mp4", "jpg") or not os.path.isfile(v.path(ext)):
        raise Http404
    resp = FileResponse(open(v.path(ext), "rb"), content_type="video/mp4" if ext == "mp4" else "image/jpeg")
    if request.GET.get("dl"):
        resp["Content-Disposition"] = 'attachment; filename="%s"' % v.file_name
    return resp


# ---------------- Croissance : bandeau du site (promo, maintenance, info + compte a rebours) ----------------
def _banner_form():
    from django import forms
    from .models import SiteBanner
    dt = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")

    class BannerForm(forms.ModelForm):
        class Meta:
            model = SiteBanner
            fields = ["name", "kind", "text_fr", "text_en", "code", "link_url", "starts_at", "ends_at",
                      "countdown_to", "active"]
            widgets = {"starts_at": dt, "ends_at": dt, "countdown_to": dt}
    return BannerForm


def _bank_filter(params):
    """Images de la banque selon les filtres de la page (GET) -> queryset ordonne (ou liste si filtre de format)."""
    from django.db.models import Q
    from .models import StockImage
    qs = StockImage.objects.exclude(status="deleted")
    status = params.get("status", "new")
    if status == "used":
        qs = qs.exclude(used_in="")
    elif status in dict(StockImage.STATUS):
        qs = qs.filter(status=status)
    cat, prov, q = params.get("cat", ""), params.get("prov", ""), (params.get("q") or "").strip()
    try:
        vmin = float(params.get("min") or 0)
    except ValueError:
        vmin = 0
    if cat:
        qs = qs.filter(category=cat)
    if prov:
        qs = qs.filter(provider=prov)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(tags__icontains=q) | Q(query__icontains=q))
    if vmin:
        qs = qs.filter(vibrancy__gte=vmin)
    if not params.get("people"):
        qs = qs.filter(people=False)
    qs = qs.order_by({"size": "-width", "recent": "-created_at", "oldest": "created_at"}.get(params.get("sort"), "-vibrancy"),
                     "-pk")
    orient = params.get("orient", "")
    return [i for i in qs if i.orientation == orient] if orient else qs


@staff_member_required
def erp_images(request):
    """Banque d'images libres : collecte (Openverse, Wikimedia, Pixabay, Pexels), tri, envoi vers catalogue / videos / studio."""
    import uuid as _uuid
    from decimal import Decimal, InvalidOperation
    from django.contrib import admin as _admin
    from django.contrib import messages
    from django.core.paginator import Paginator
    from django.db.models import Count, Q
    from . import imagebank, satisfying
    from .models import StockImage
    if request.method == "POST":
        op = request.POST.get("op")
        img = StockImage.objects.filter(pk=request.POST.get("id")).first()
        back = request.POST.get("next") or request.get_full_path()

        def selected():
            """Images choisies : cases cochees, ou toutes celles du filtre courant (toutes pages)."""
            from django.http import QueryDict
            if request.POST.get("all"):
                return [i.pk for i in _bank_filter(QueryDict(request.POST.get("filter_qs", "")))]
            return [int(x) for x in request.POST.getlist("ids") if x.isdigit()] or ([img.pk] if img else [])
        if op == "harvest":
            cats = [c for c in request.POST.getlist("cat") if c in imagebank.THEMES] or None
            provs = [p for p in request.POST.getlist("prov") if p in imagebank.available()] or None
            try:
                per = max(5, min(80, int(request.POST.get("per_query") or 30)))
            except ValueError:
                per = 30
            if imagebank.harvest_async(categories=cats, providers=provs, per_query=per):
                messages.success(request, "Collecte lancée en arrière-plan (%d recherches). Les images arrivent au fil de l'eau."
                                 % imagebank.total_queries(cats, provs))
            else:
                messages.error(request, "Une collecte est déjà en cours.")
        elif op in ("bulk_gallery", "bulk_video"):
            ids = selected()
            try:
                colors = int(request.POST.get("colors") or 24)
            except ValueError:
                colors = 24
            opts = {"colors": colors if colors in (12, 18, 24, 30, 36, 48) else 24}
            if op == "bulk_gallery":
                try:
                    pr = max(Decimal("0"), Decimal((request.POST.get("price") or "0").replace(",", ".")))
                except InvalidOperation:
                    pr = Decimal("0")
                opts.update(price=str(pr), density=request.POST.get("density", ""))
            else:
                from . import shorts
                fmt = request.POST.get("fmt") if request.POST.get("fmt") in ("fill", "guess", "tips") else "fill"
                style = request.POST.get("music_style") if request.POST.get("music_style") in dict(satisfying.MUSIC_STYLES) else "ambient"
                opts.update(fmt=fmt, music_style=style, hooks=[h for _g, lines in HOOKS for h in lines])
            if not ids:
                messages.error(request, "Aucune image sélectionnée.")
            elif imagebank.bulk_async("gallery" if op == "bulk_gallery" else "video", ids, opts):
                messages.success(request, "%d image(s) en cours de traitement en arrière-plan (environ 20 à 40 s par image)%s."
                                 % (len(ids), "" if op == "bulk_gallery" else ", puis rendu des vidéos dans « Vidéos TikTok »"))
            else:
                messages.error(request, "Un traitement par lot est déjà en cours : attendez qu'il se termine.")
        elif op == "delete":
            # suppression = vignette effacee + fiche gardee en « supprimee » : une collecte future ne la reimporte pas
            ids = selected()
            gone = StockImage.objects.filter(pk__in=ids)
            for i in gone:
                if os.path.exists(i.thumb_path()):
                    os.remove(i.thumb_path())
            n = gone.update(status="deleted")
            messages.success(request, "%d image(s) supprimée(s)." % n)
        elif op in ("kept", "rejected", "new"):
            ids = selected()
            n = StockImage.objects.filter(pk__in=ids).exclude(status="deleted").update(status=op)
            messages.success(request, "%d image(s) : %s." % (n, dict(StockImage.STATUS)[op].lower()))
        elif op in ("catalogue", "video", "studio") and img:
            try:
                hd = imagebank.fetch_hd(img)
                try:
                    if op == "catalogue":
                        try:
                            pr = max(Decimal("0"), Decimal((request.POST.get("price") or "0").replace(",", ".")))
                        except InvalidOperation:
                            pr = Decimal("0")
                        m = _create_gallery_model((request.POST.get("title") or img.title).strip()[:80],
                                                  (request.POST.get("category") or img.category).strip()[:40], pr,
                                                  colors=int(request.POST.get("colors") or 24),
                                                  orientation="paysage" if img.orientation == "paysage" else "portrait",
                                                  image_path=hd, in_slider=bool(request.POST.get("in_slider")),
                                                  zones=_zones(request.POST.get("density")))
                        imagebank.mark_used(img, "catalogue " + m.uid)
                        messages.success(request, "Modèle « %s » publié dans la galerie." % m.title)
                    else:
                        from .forms import DENSITY_LEVELS
                        from .pipeline import generate
                        uid = ("vid-" if op == "video" else "mkt-") + _uuid.uuid4().hex[:10]
                        dens, det = DENSITY_LEVELS["auto"]
                        generate(hd, 24, 40, 50, uid=uid, source_name="stock-%d.jpg" % img.pk, density=dens, detail=det)
                        imagebank.mark_used(img, ("vidéo " if op == "video" else "studio ") + uid)
                        if op == "video":
                            satisfying.save_label(uid, "Banque · %s" % img.title[:80])
                            messages.success(request, "Toile prête : elle est sélectionnée en tête de liste.")
                            return redirect("/admin-hub/videos/?canvas=%s" % uid)
                        return redirect("/marketing/?img=%s" % uid)
                finally:
                    if os.path.exists(hd):
                        os.remove(hd)
            except Exception as exc:
                logger.exception("Banque d'images %s", img.pk)
                messages.error(request, "Impossible d'utiliser cette image : %s" % exc)
        return redirect(back)

    status, cat = request.GET.get("status", "new"), request.GET.get("cat", "")
    prov, q, orient = request.GET.get("prov", ""), (request.GET.get("q") or "").strip(), request.GET.get("orient", "")
    sort = request.GET.get("sort", "vibrancy")
    qs = _bank_filter(request.GET)
    page = Paginator(qs, 60).get_page(request.GET.get("page"))
    n_pages, cur = page.paginator.num_pages, page.number
    shown = sorted({1, n_pages, *range(max(1, cur - 2), min(n_pages, cur + 2) + 1)})
    page_links = []                                   # 1 … 4 5 [6] 7 8 … 40
    for k, num in enumerate(shown):
        if k and num - shown[k - 1] > 1:
            page_links.append(None)
        page_links.append(num)
    allq = StockImage.objects.exclude(status="deleted")
    counts = {"new": allq.filter(status="new").count(), "kept": allq.filter(status="kept").count(),
              "rejected": allq.filter(status="rejected").count(), "used": allq.exclude(used_in="").count()}
    params = request.GET.copy()
    params.pop("page", None)
    for i in page.object_list:
        i.note = imagebank.LICENSE_NOTES.get(i.provider, "")
    by_cat = dict(allq.values_list("category").annotate(n=Count("id")))
    return render(request, "admin/erp_images.html", {
        "tabs": [(k, l, counts[k]) for k, l in (("new", "À trier"), ("kept", "Retenues"), ("used", "Utilisées"),
                                                ("rejected", "Archivées")) if k != "rejected" or counts[k]],
        "prov_opts": [(k, l, imagebank.LICENSE_NOTES[k], k in imagebank.available()) for k, l in StockImage.PROVIDERS],
        "cat_opts": [(c, by_cat.get(c, 0)) for c in imagebank.THEMES], "mins": ["40", "50", "60", "70"],
        **_admin.site.each_context(request), "page": page, "status": status, "counts": counts, "total": allq.count(),
        "themes": imagebank.THEMES, "providers": StockImage.PROVIDERS,
        "f": {"cat": cat, "prov": prov, "q": q, "min": request.GET.get("min", ""), "orient": orient, "sort": sort,
              "people": request.GET.get("people", "")},
        "qs_nopage": params.urlencode(), "state": imagebank.state(), "page_links": page_links,
        "bulk": imagebank.bulk_state(), "filtered": page.paginator.count, "filter_qs": params.urlencode(),
        "formats": [f for f in __import__("studio.shorts", fromlist=["x"]).FORMATS if f[0] != "versus"],
        "music_styles": satisfying.MUSIC_STYLES,
        "params_nopage": [(k, v) for k in params for v in params.getlist(k)],
        "erp_section": "growth", "gtab": "images"})


@staff_member_required
def erp_images_swipe(request):
    """Tri express facon Tinder : droite = garder (galerie), gauche = archiver. Une image a la fois, clavier ou glisser."""
    import json as _json
    from django.contrib import admin as _admin
    from .models import StockImage
    todo = StockImage.objects.filter(status="new")
    if not request.GET.get("people"):
        todo = todo.filter(people=False)
    if request.method == "POST":
        try:
            d = _json.loads(request.body or b"{}")
        except ValueError:
            d = {}
        st = d.get("status")
        if st not in ("kept", "rejected", "new"):
            return JsonResponse({"ok": False}, status=400)
        n = StockImage.objects.filter(pk=d.get("id")).exclude(status="deleted").update(status=st)
        return JsonResponse({"ok": bool(n), "left": todo.count()})
    if request.GET.get("format") == "json":                   # lot suivant (les ids deja en file sont exclus)
        skip = [int(x) for x in (request.GET.get("skip") or "").split(",") if x.isdigit()]
        cat = request.GET.get("cat", "")
        qs = todo.exclude(pk__in=skip)
        if cat:
            qs = qs.filter(category=cat)
        items = [{"id": i.pk, "title": i.title, "cat": i.category, "lic": i.license or i.get_provider_display(),
                  "src": i.get_provider_display(), "wh": "%d×%d" % (i.width, i.height), "v": round(i.vibrancy),
                  "page": i.page_url, "people": i.people}
                 for i in qs.order_by("-vibrancy", "pk")[:30]]
        return JsonResponse({"items": items, "left": (todo.filter(category=cat) if cat else todo).count()})
    from . import imagebank
    from django.db.models import Count
    by_cat = dict(todo.values_list("category").annotate(n=Count("id")))
    return render(request, "admin/erp_images_swipe.html", {
        **_admin.site.each_context(request), "left": todo.count(),
        "kept": StockImage.objects.filter(status="kept").count(),
        "cats": [(c, by_cat.get(c, 0)) for c in imagebank.THEMES if by_cat.get(c)],
        "erp_section": "growth", "gtab": "images"})


@staff_member_required
def erp_image_thumb(request, pk):
    from .models import StockImage
    img = StockImage.objects.filter(pk=pk).first()
    if not img or not os.path.exists(img.thumb_path()):
        raise Http404
    resp = FileResponse(open(img.thumb_path(), "rb"), content_type="image/jpeg")
    resp["Cache-Control"] = "private, max-age=86400"
    return resp


@staff_member_required
def erp_banners(request):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from .models import SiteBanner
    Form = _banner_form()
    edit = SiteBanner.objects.filter(pk=request.GET.get("edit") or request.POST.get("id") or 0).first()
    form = Form(instance=edit)
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "save":
            form = Form(request.POST, instance=edit)
            if form.is_valid():
                b = form.save()
                messages.success(request, "Bandeau « %s » enregistré%s." % (b.name, " et en ligne" if b.is_live() else ""))
                return redirect(request.path)
        elif edit and op == "toggle":
            edit.active = not edit.active; edit.save()
            return redirect(request.path)
        elif edit and op == "delete":
            edit.delete()
            return redirect(request.path)
    now = timezone.now()
    live = SiteBanner.current()
    banners = list(SiteBanner.objects.all())
    for b in banners:
        b.state = ("live" if live and b.pk == live.pk else "off" if not b.active else
                   "ended" if b.ends_at and b.ends_at <= now else
                   "planned" if b.starts_at and b.starts_at > now else "masked")
    return render(request, "admin/erp_banners.html", {
        **_admin.site.each_context(request), "banners": banners, "live": live, "form": form, "edit": edit,
        "erp_section": "growth", "gtab": "banner"})


# ---------------- Mesure d'audience interne (sans cookie) ----------------
@csrf_exempt
def analytics_collect(request):
    from . import analytics
    if request.method != "POST" or len(request.body) > 4000:
        return HttpResponse(status=204)
    if security.rate_limited("a:" + security.client_ip(request), 600, 3600):
        return HttpResponse(status=204)
    try:
        out = analytics.collect(request)
    except Exception:
        logger.exception("Mesure d'audience")
        out = {}
    return JsonResponse(out) if out else HttpResponse(status=204)


@staff_member_required
def erp_activity(request):
    """Activite du site : visiteurs, pays, pages d'entree, temps passe, pages vues, clics CTA, generations."""
    from django.contrib import admin as _admin
    from . import analytics
    days = request.GET.get("p", "30")
    days = int(days) if days in dict(analytics.PERIODS) else 30
    return render(request, "admin/erp_activity.html", {
        **_admin.site.each_context(request), "a": analytics.dashboard(days), "erp_section": "activity"})
