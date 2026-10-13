import datetime
import os
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
from .models import Order, OrderEvent, Pricing, DigitalCanvas, EmailCode
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
    lib = list(DigitalCanvas.objects.filter(email="__library__", uid__startswith="gal-", price=0)
               .order_by("?")[:12])
    from django.templatetags.static import static as _static
    lib_slugs = dict(DigitalCanvas.objects.filter(email="__library__").exclude(showcase_slug="")
                     .values_list("showcase_slug", "in_slider"))
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
        if form.is_valid():
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
    from . import seo
    return render(request, "studio/upload.html", {
        "ld_product": seo.dump(seo.product()),
        "form": form, "price_cfg": price_cfg(), "has_photo": has_photo,
        "focus_x": "%.4f" % fx, "focus_y": "%.4f" % fy,
    })


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
    return FileResponse(open(p, "rb"))


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
    if lib:
        verified = request.session.get("verified_email")
        owns = bool(verified) and DigitalCanvas.objects.filter(email=verified, uid=uid).exists()
        if float(lib.price or 0) > 0 and not owns:
            return redirect("studio:gallery_buy", uid=uid)
        if verified and not owns:
            _grant_gallery(request, lib, verified)   # gratuite -> arrive dans Mes Toiles
    owned = DigitalCanvas.objects.filter(uid=uid).exists()
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
                  {"uid": uid, "palette": palette, "owned": owned, "back_url": back_url, "back_label": back_label})


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
                        "ratio": round(fw / fh, 3), "price": round(float(kf.price) + p.color_price(colors), 2)})
    gw, gh = order.get("gen_w") or order.get("width_cm"), order.get("gen_h") or order.get("height_cm")
    gen_ratio = round(float(gw) / float(gh), 3) if gw and gh else None
    current_format = next((fm["id"] for fm in formats
                           if float(fm["w"]) == float(order.get("width_cm") or 0)
                           and float(fm["h"]) == float(order.get("height_cm") or 0)),
                          (formats[0]["id"] if formats else None))
    # aligne le prix affiche sur le format courant
    if formats:
        cf = next((fm for fm in formats if fm["id"] == current_format), formats[0])
        order["canvas_price"] = cf["price"]
        order["width_cm"], order["height_cm"], order["format_label"] = cf["w"], cf["h"], cf["label"]
        order["price"] = round(cf["price"] + order.get("brushes_amount", 0.0), 2)
        request.session["order"] = order
    # Difficulte reelle de CETTE toile (zones + couleurs), comme les etoiles de la page Jouer
    from .models import difficulty_level
    if not order.get("zones"):
        order["zones"] = _count_zones(order.get("uid", ""))
        request.session["order"] = order
    level = difficulty_level(order.get("zones"), colors)
    return render(request, "studio/preview.html",
                  {"order": order, "brushes_price": p.brushes_price, "formats": formats, "level": level,
                   "gen_ratio": gen_ratio, "gen_w": gw, "gen_h": gh,
                   "current_format": current_format,
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
        if fmt_id:
            kf = KitFormat.objects.filter(id=fmt_id, available=True).first()
            if kf:
                a, b = sorted((kf.width_cm, kf.height_cm))
                fw, fh = (b, a) if order.get("orientation") == "paysage" else (a, b)
                order["width_cm"], order["height_cm"] = fw, fh
                order["format_label"] = "%d x %d cm" % (fw, fh)
                order["canvas_price"] = round(float(kf.price) + p.color_price(order.get("colors", 24)), 2)
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


def order_file(request, uid, name):
    """Fichier de media/orders/<uid>/ via lien signe (fournisseurs) ou pour le staff.
    Le dossier n'est plus servi publiquement par nginx (donnees clients)."""
    if "/" in name or "\\" in name or name.startswith(".") or "/" in uid or uid.startswith("."):
        raise Http404
    if not (request.user.is_staff or security.check_file_token(uid, name, request.GET.get("t", ""))):
        raise Http404
    if name == "production.zip":
        # Tous les fichiers de production d'une commande en un seul lien (fournisseur / ERP) - sans prix
        import io, zipfile
        # Ce dont le fournisseur a besoin, et seulement ca : poster, toile .tiff, JSON (couleurs -> references nuancier)
        import json as _json
        d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
        names = ["%s_poster.png" % uid, "%s_template.tiff" % uid, "%s_supplier.json" % uid]
        found = [n for n in names if os.path.isfile(os.path.join(d, n))]
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
                      "color_specifications": __import__("studio.fulfillment", fromlist=["x"]).supplier_colors(
                          full.get("color_specifications") or [])}
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
    canvases = list(DigitalCanvas.objects.filter(email=verified)) if verified else []
    free_used = (len(canvases) >= 1) if verified else bool(request.session.get("free_used"))
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
    models = list(DigitalCanvas.objects.filter(email="__library__", uid__startswith="gal-").order_by("category", "title"))
    for m in models:   # nb de zones calcule une fois depuis le SVG du jeu, puis memorise
        if not m.zones:
            m.zones = _count_zones(m.uid)
            if m.zones:
                m.save(update_fields=["zones"])
    from . import seo
    from django.utils.translation import gettext as _t
    return render(request, "studio/gallery.html", {
        "models": models,
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
    _bp = request.session.pop("buy_pending", None)
    return JsonResponse({"ok": True, "redirect": ("/play/buy/%s/" % _bp) if _bp else "/paint/"})


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


def _credit_digital(request, uid):
    """Enregistre/deverrouille la toile numerique dans la galerie de l'utilisateur."""
    verified = request.session.get("verified_email", "")
    _record_digital(uid, verified, request.session.get("order") or {})
    request.session["free_used"] = True
    if verified:
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
        DigitalCanvas.objects.update_or_create(
            email=verified, uid=uid,
            defaults=dict(colors=colors, orientation=ori, width_cm=w, height_cm=h, source="digital"))
    except Exception:
        logger.exception("Toile numerique %s", uid)


def paint_confirm(request, uid):
    """Mur de paiement 'pour continuer' (0,99 EUR). Stripe si configure, sinon demo immediat."""
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    if payments.stripe_live():
        try:
            url = payments.create_digital_session(
                uid, request, email=request.session.get("verified_email") or None)
            return JsonResponse({"ok": True, "redirect": url})
        except Exception as exc:
            logger.exception("Stripe digital session %s", uid)
            return JsonResponse({"ok": False, "error": str(exc)}, status=502)
    if not payments.demo_allowed():
        return JsonResponse({"ok": False, "error": "paiement indisponible"}, status=503)
    # Demo (dev uniquement) : pas de paiement reel -> credit immediat.
    _credit_digital(request, uid)
    return JsonResponse({"ok": True})


def paint_unlock_success(request):
    """Retour Stripe apres paiement de la toile numerique : credite puis ouvre le jeu."""
    uid = request.GET.get("uid", "")
    sid = request.GET.get("sid", "")
    if uid and payments.paid_session(sid, uid=uid, kind="digital"):
        _credit_digital(request, uid)
    return redirect("studio:digipaint", uid=uid)


def paint_delete(request, uid):
    if request.method != "POST":
        return redirect("studio:paint_portal")
    verified = request.session.get("verified_email", "")
    DigitalCanvas.objects.filter(uid=uid, email=verified).delete()
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
    gameuid = uid + "-G"
    jobs.start(gameuid)
    try:
        genqueue.submit(_run_game_generation, gameuid, src, colors, w_cm, h_cm, detail,
                        source_name=os.path.basename(src), max_zones=max_zones,
                        min_zone_mm=min_zone_mm, density=density)
    except genqueue.QueueFull:
        jobs.pop(gameuid)
        return JsonResponse({"error": "busy"}, status=429)
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
    body = (
        "User-agent: *\n"
        "Disallow: /preview/\nDisallow: /media/\nDisallow: /files/\n"
        "Disallow: /delivery/\nDisallow: /checkout/\nDisallow: /order/\nDisallow: /admin/\nDisallow: /admin-hub/\nDisallow: /go/\nDisallow: /avis/\nDisallow: /wc-api/\nDisallow: /wp-json/\n"
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
    return render(request, "studio/delivery.html", {"form": form, "order": order, "msg": msg})


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
    return render(request, "studio/checkout.html", {
        "order": order, "shipping": shipping, "discount": discount, "total": total,
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
    o["lang"] = (get_language() or o.get("lang", "fr"))[:2]   # langue du process (achat)
    o["ad_ref"] = request.session.get("ad_ref", "")             # A/B marketing : pub d'origine

    if payments.stripe_live():
        _upsert_order(o, shipping, status=Order.PENDING)
        try:
            url = payments.create_checkout_session(o, shipping, request)
        except Exception as exc:
            return render(request, "studio/checkout.html", {
                "order": order, "shipping": shipping, "discount": discount, "total": total,
                "stripe": True, "msg": {"kind": "err", "text": f"Stripe indisponible : {exc}"}})
        return redirect(url)

    if not payments.demo_allowed():   # prod sans Stripe : on ne livre JAMAIS sans paiement
        logger.error("Commande refusee : Stripe non configure (STRIPE_SECRET_KEY)")
        return render(request, "studio/checkout.html", {
            "order": order, "shipping": shipping, "discount": discount, "total": total,
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
        "supplier": supplier_result, "manifest": manifest, "uid": o["uid"],
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
            "canvas_price": canvas, "price": r.price, "discount": discount, "total": r.total,
            "lang": r.lang}


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
        "total": o["total"], "supplier": {"status": "ok", "supplier_ref": r.supplier_ref},
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
        if uid and kind == "digital":
            _record_digital(uid, meta.get("email", ""), {})
        elif uid and kind == "gallery":
            m = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
            if m and meta.get("email"):
                DigitalCanvas.objects.get_or_create(
                    email=meta["email"], uid=uid,
                    defaults=dict(title=m.title, category=m.category, colors=m.colors, orientation=m.orientation,
                                  width_cm=m.width_cm, height_cm=m.height_cm, source="library", price=0))
        elif uid:
            r = Order.objects.filter(uid=uid).first()
            paid = (obj.get("amount_total") or 0) / 100.0
            if r and r.status == Order.PENDING and paid + 0.01 < float(r.total or 0):
                logger.error("Montant Stripe %.2f < total commande %.2f (%s)", paid, r.total, uid)
                _order_note(r, "ALERTE : montant paye %.2f EUR < total %.2f EUR, non traitee" % (paid, r.total))
            else:
                _ensure_fulfilled(uid, "(webhook Stripe)")
    elif etype in ("charge.refunded", "charge.dispute.created"):
        uid = meta.get("uid")
        if not uid and obj.get("payment_intent"):
            try:
                import stripe as _stripe
                _stripe.api_key = settings.STRIPE_SECRET_KEY
                uid = (_stripe.PaymentIntent.retrieve(obj["payment_intent"]).get("metadata") or {}).get("uid")
            except Exception:
                logger.exception("Stripe %s", etype)
        r = Order.objects.filter(uid=uid).first() if uid else None
        if etype == "charge.refunded":
            from . import ledger
            kind = meta.get("kind") or ("kit" if r else "digital")
            refunds = ((obj.get("refunds") or {}).get("data")) or []
            for rf in refunds:
                ledger.record_refund(rf.get("id"), (rf.get("amount") or 0) / 100.0, uid=uid or "", kind=kind,
                                     payment_intent=obj.get("payment_intent") or "")
            if not refunds:
                ledger.record_refund("%s-%s" % (obj.get("id"), obj.get("amount_refunded")),
                                     (obj.get("amount_refunded") or 0) / 100.0, uid=uid or "", kind=kind,
                                     payment_intent=obj.get("payment_intent") or "")
        if r:
            label = "Remboursement Stripe" if etype == "charge.refunded" else "LITIGE Stripe ouvert"
            _order_note(r, "%s : %.2f EUR" % (label, (obj.get("amount_refunded") or obj.get("amount") or 0) / 100.0))
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
    made = " Les kits sont fabriqués dans nos ateliers."
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
        "- Algorithme de précision : zones réellement peignables, numéros lisibles, couleurs choisies parmi de vraies peintures disponibles chez l'atelier.",
        "- Qualité premium : kits fabriqués dans nos ateliers, livraison offerte.",
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
    lines += ["", "## Liens",
              "- Créer une toile : %s/create/" % site, "- Tester le jeu : %s/play/" % site,
              "- FAQ : %s/faq/" % site, "- Contact : %s/contact/" % site,
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
                       "avant d'acheter, algorithme de précision, kit de qualité fabriqué dans nos ateliers et livré gratuitement.",
        "positioning": ["personnalisation totale (pas de catalogue)", "tester avant d'acheter (aperçu et jeu gratuits)",
                        "transparence des prix", "algorithme de précision (vraies peintures disponibles)",
                        "qualité premium, fabrication dans nos ateliers"],
        "audience": {"min_age": 4, "note": "12 couleurs : très accessible pour les enfants et les seniors"},
        "offer": {"formats": f["formats"], "colors": f["colors"], "price_from_eur": round(f["price_from"], 2),
                  "shipping": "offerte", "delivery_days": [f["dmin"], f["dmax"]],
                  "kit": ["toile numérotée", "pots de peinture numérotés", "poster de référence", "pinceaux (option)"]},
        "use_cases": ["cadeau personnalisé", "portrait d'enfant ou d'animal", "souvenir de voyage",
                      "activité créative en famille", "activité pour seniors", "décoration murale"],
        "languages": ["fr", "en", "de", "es", "it"],
        "faq": f["faq"],
        "links": {"home": site + "/", "create": site + "/create/", "play": site + "/play/",
                  "faq": site + "/faq/", "contact": site + "/contact/", "sitemap": site + "/sitemap.xml"},
    }
    return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})


def sitemap_xml(request):
    from django.http import HttpResponse
    from django.urls import translate_url
    from .models import DigitalCanvas
    site = settings.SITE_URL
    # pages publiques uniquement (les toiles /play/<uid>/ ne sont pas indexees)
    paths = ["/", "/create/", "/play/", "/faq/", "/contact/", "/cgv/", "/mentions-legales/", "/privacy/"]
    langs = [c for c, _ in settings.LANGUAGES]
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
            x.append("<url><loc>%s</loc>%s<changefreq>weekly</changefreq></url>" % (variants[c], alts))
    x.append("</urlset>")
    resp = HttpResponse("\n".join(x), content_type="application/xml; charset=utf-8")
    resp["Cache-Control"] = "public, max-age=3600"
    return resp


def shop_pricing(request):
    from django.contrib.admin.views.decorators import staff_member_required
    from django.contrib import messages
    from .models import PrintPricing
    if not request.user.is_staff:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())
    kit = Pricing.get(); tab = PrintPricing.get()
    from .models import Supplier, CompanyInfo, KitFormat as _KF
    sup = Supplier.for_checkout("kit")
    if request.method == "POST" and request.POST.get("op") == "align" and sup:
        if not any(sup.offer_get("formats", kf.key).get("on") for kf in _KF.objects.all()):
            messages.error(request, "L'offre de %s ne contient aucun format : saisissez-la d'abord sur sa fiche "
                                    "(Modifier → Offre & tarifs)." % sup.name)
            return redirect("studio:shop_pricing")
        # Disponibilites du Kit = ce que le fournisseur actif propose
        for kf in _KF.objects.all():
            kf.available = bool(sup.offer_get("formats", kf.key).get("on")); kf.save(update_fields=["available"])
        for n in (12, 24, 36, 48):
            setattr(kit, "av_c%d" % n, bool(sup.offer_get("colors", n).get("on")))
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


@staff_member_required
def admin_hub(request):
    from . import erp
    from .models import Order, ContactMessage
    period = request.GET.get("p", "30d")
    k = erp.kpis(period)
    ctx = {"k": k, "series": erp.series(k["period"]), "mix": erp.mix(k["period"]),
           "pipeline": erp.pipeline(), "actions": erp.actions(), "cust": erp.customer_stats(),
           "recent_orders": list(Order.objects.order_by("-created_at")[:8]),
           "recent_msgs": list(ContactMessage.objects.order_by("answered", "-created_at")[:6]),
           "gallery_all": erp.gallery_sales(), "erp_section": "hub"}
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
    """Ajoute le modele achete aux digipaints de l'utilisateur."""
    DigitalCanvas.objects.get_or_create(
        email=email, uid=m.uid,
        defaults=dict(title=m.title, category=m.category, colors=m.colors,
                      orientation=m.orientation, width_cm=m.width_cm, height_cm=m.height_cm,
                      source="library", price=0))
    uids = request.session.get("my_uids", [])
    if m.uid not in uids:
        uids.append(m.uid); request.session["my_uids"] = uids[-60:]


def gallery_buy(request, uid):
    m = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
    if not m:
        raise Http404
    price = float(m.price or 0)
    if price <= 0:
        return redirect("studio:digipaint", uid=uid)
    verified = request.session.get("verified_email")
    if not verified:
        request.session["buy_pending"] = uid          # on l'achete apres identification
        return redirect("studio:paint_portal")
    if DigitalCanvas.objects.filter(email=verified, uid=uid).exists():
        return redirect("studio:digipaint", uid=uid)   # deja possede
    if payments.stripe_live():
        try:
            return redirect(payments.create_gallery_session(uid, request, price, verified))
        except Exception:
            logger.exception("Stripe galerie %s", uid)
            return JsonResponse({"error": "paiement indisponible"}, status=502)
    if not payments.demo_allowed():
        return JsonResponse({"error": "paiement indisponible"}, status=503)
    _grant_gallery(request, m, verified)               # demo (dev) : credit immediat
    return redirect("studio:digipaint", uid=uid)


def gallery_buy_success(request):
    uid = request.GET.get("uid", ""); sid = request.GET.get("sid", "")
    verified = request.session.get("verified_email")
    if uid and verified and payments.paid_session(sid, uid=uid, kind="gallery"):
        m = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
        if m:
            _grant_gallery(request, m, verified)
    return redirect("studio:digipaint", uid=uid)


# ---------------- Fournisseurs (tableau de bord facon Shopify) ----------------
def _sup_color(name):
    import hashlib
    cols = ["#2f6bf2", "#7a3ff2", "#1e8848", "#e8703b", "#e85d75", "#0f9bb3", "#b3287a", "#9a6300"]
    return cols[int(hashlib.md5((name or "?").encode()).hexdigest(), 16) % len(cols)]


SUPPLIER_CHANNELS = [
    ("woo", "MangoERP", "Son ERP récupère nos commandes"),
    ("shopify", "Shopify", "Via votre boutique Shopify"),
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
    sup.channel = ("Shopify" if sup.integration == "shopify" and (sup.shopify or {}).get("domain") else
                   "MangoERP" if sup.integration == "woo" and (sup.woo or {}).get("ck") else
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
        if sup and request.POST.get("op") == "toggle":
            sup.active = not sup.active; sup.save(update_fields=["active"])
        return redirect("studio:erp_suppliers")
    sups = [_sup_card(s) for s in Supplier.objects.all()]
    routing = {c: Supplier.for_checkout(c) for c in ("kit", "print")}
    tot = {"active": sum(1 for s in sups if s.active),
           "orders_30": sum(s.stats["orders_30"] for s in sups),
           "spend_30": round(sum(s.stats["spend_30"] for s in sups), 2),
           "in_production": sum(s.stats["in_production"] for s in sups),
           "late": sum(s.stats["late"] for s in sups),
           "errors": sum(1 for s in sups if s.health == "err")}
    return render(request, "admin/erp_suppliers.html", {
        **_admin.site.each_context(request), "sups": sups, "tot": tot, "routing": routing,
        "unassigned": erp.unassigned_orders().count(), "erp_section": "suppliers"})


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
        if op == "toggle":
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
        elif op in ("shop_test", "shop_catalog", "shop_webhooks", "shop_poll", "shop_push"):
            from . import shopify
            try:
                if op == "shop_test":
                    shop = shopify.test(sup)
                    messages.success(request, "Connexion Shopify OK : %s (%s, %s)." % (
                        shop.get("name"), shop.get("myshopifyDomain"), shop.get("currencyCode")))
                elif op == "shop_catalog":
                    n = shopify.sync_catalogue(sup)
                    messages.success(request, "Catalogue publié dans Shopify : %d variante(s) avec SKU. "
                                     "Le fournisseur les associe à ses produits dans son ERP (配对 SKU)." % n)
                elif op == "shop_webhooks":
                    t = shopify.install_webhooks(sup)
                    messages.success(request, "Retours automatiques activés : %s." % ", ".join(t))
                elif op == "shop_poll":
                    n = shopify.poll_tracking(sup)
                    messages.success(request, "%d commande(s) mises à jour depuis Shopify." % n)
                elif op == "shop_push":
                    n = ko = 0
                    for o in Order.objects.filter(supplier=sup, shopify_order_id="",
                                                  status__in=(Order.PAID, Order.FULFILLED)):
                        if _notify_supplier(_order_from_row(o), _shipping_from_row(o), sup=sup, user=user):
                            n += 1
                        else:
                            ko += 1
                    (messages.success if not ko else messages.error)(
                        request, "%d commande(s) créée(s) dans Shopify%s." % (n, (", %d échec(s) (voir journal)" % ko) if ko else ""))
            except Exception as exc:
                messages.error(request, "Shopify : %s" % exc)
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
    tabs = [("open", "À traiter", qs.filter(status__in=(Order.PAID, Order.FULFILLED))),
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
        late = (o.status == Order.FULFILLED and age >= sup.stats["lead"]) or \
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
    due = list(Order.objects.filter(supplier=sup, status__in=(Order.FULFILLED, Order.SHIPPED, Order.DELIVERED),
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
        "woo": sup.woo or {}, "woo_site": settings.SITE_URL,
        "woo_logs": (ConnectorLog.objects.filter(models.Q(supplier=sup) | models.Q(
                         supplier__isnull=True, at__gte=timezone.now() - timedelta(days=7)))[:20]
                     if sup.integration == "woo" else []),
        "shop": sup.shopify or {}, "shop_hook": __import__("studio.shopify", fromlist=["x"]).webhook_url(sup),
        "shop_pending": Order.objects.filter(supplier=sup, shopify_order_id="", status__in=(Order.PAID, Order.FULFILLED)).count(),
        **_admin.site.each_context(request), "sup": sup, "tab": tab, "rows": rows,
        "tabs": [(k, l, q.count()) for k, l, q in tabs], "events": events,
        "iban": mask(sup.iban), "api_key": mask(sup.api_key, 3),
        "files": [lbl for flag, lbl in [("want_template_pdf", "Toile numérotée PDF vectoriel"),
                                        ("want_source", "Photo source"), ("want_template_svg", "Toile numérotée .svg"),
                                        ("want_template_tiff", "Toile numérotée .tiff"),
                                        ("want_preview_svg", "Aperçu colorié .svg"), ("want_poster", "Poster PNG"),
                                        ("want_order_json", "Fiche JSON (sans prix)")] if getattr(sup, flag, False)],
        "unassigned": erp.unassigned_orders().count(), "erp_section": "suppliers"})


SUPPLIER_SECTIONS = {
    "profile": ["name", "checkout", "priority", "lead_time_days", "incoterms", "production_notes", "terms"],
    "contact": ["contact_name", "contact_email", "phone", "address", "bank_name", "iban", "bic", "notes"],
    "channel": ["email", "api_url", "api_key"],
    "dispatch": ["auto_dispatch", "want_template_tiff", "want_poster", "want_order_json", "want_template_pdf",
                 "want_template_svg", "want_preview_svg", "want_source"],
    "excel": [], "palette": [], "docs": [],
}
SUPPLIER_SECTION_EXTRAS = {"channel": ("shopify",), "excel": ("xlsx",), "palette": ("palette",), "docs": ("files",)}
SUPPLIER_LABELS = {
    "name": "Nom", "checkout": "Produit fourni", "priority": "Priorité", "lead_time_days": "Délai de production (jours)",
    "incoterms": "Incoterm", "production_notes": "Consignes de production", "terms": "Conditions commerciales",
    "contact_name": "Interlocuteur", "contact_email": "E-mail", "phone": "Téléphone", "address": "Adresse",
    "bank_name": "Banque", "iban": "IBAN", "bic": "BIC / SWIFT", "notes": "Notes internes",
    "email": "E-mail de réception des commandes", "api_url": "URL de l'API", "api_key": "Clé API",
    "auto_dispatch": "Transmettre automatiquement chaque commande payée",
    "want_template_tiff": "Toile numérotée (TIFF)", "want_poster": "Poster couleur (PNG)",
    "want_order_json": "Fiche couleurs (JSON)", "want_template_pdf": "Toile numérotée (PDF)",
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
            or sup.integration == "woo" and not (sup.woo or {}).get("ck") \
            or sup.integration == "shopify" and not (sup.shopify or {}).get("domain"):
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
    if "shopify" in parts and "shop_domain" in P:
        sh = dict(sup.shopify or {})
        new = {"domain": P.get("shop_domain", "").strip()[:120], "client_id": P.get("shop_client_id", "").strip()[:120],
               "api_version": P.get("shop_api_version", "").strip()[:12]}
        for k in ("client_secret", "static_token"):   # champs secrets : vides = inchanges
            v = P.get("shop_" + k, "").strip()
            if v:
                new[k] = v[:200]
            if P.get("shop_clear_" + k):
                new[k] = ""
        if (new["domain"], new["client_id"]) != (sh.get("domain"), sh.get("client_id")) or new.get("client_secret") \
                or "static_token" in new:
            sh.pop("token", None); sh.pop("token_exp", None)
        sh.update(new)
        sup.shopify = sh
        fields.append("shopify")
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
        rows = (pay.orders.all() if pay else qs.filter(status__in=(Order.FULFILLED, Order.SHIPPED, Order.DELIVERED),
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
            f = request.FILES.get("image")
            title = (request.POST.get("title") or "").strip()[:80]
            if not f or not title:
                messages.error(request, "Image et titre obligatoires.")
            else:
                try:
                    tmp = os.path.join(settings.MEDIA_ROOT, "uploads", "gal_%s%s" % (uuid.uuid4().hex[:8],
                                       os.path.splitext(f.name)[1] or ".jpg"))
                    os.makedirs(os.path.dirname(tmp), exist_ok=True)
                    with open(tmp, "wb") as out:
                        for chunk in f.chunks():
                            out.write(chunk)
                    _create_gallery_model(title, request.POST.get("category", "").strip(), price(request.POST.get("price")),
                                          colors=int(request.POST.get("colors") or 24),
                                          orientation=request.POST.get("orientation", "portrait"),
                                          image_path=tmp, in_slider=bool(request.POST.get("in_slider")), zones=density())
                    os.remove(tmp)
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
             "slider": lib.filter(in_slider=True).count()}
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
        "erp_section": "catalogue"})



@csrf_exempt
def shopify_webhook(request, secret):
    """Webhooks de la boutique Shopify relais (expeditions poussees par l'ERP du fournisseur)."""
    import json as _json
    from .models import Supplier
    from . import shopify
    sup = Supplier.objects.filter(webhook_secret=secret).first() if secret else None
    if not sup:
        return JsonResponse({"error": "inconnu"}, status=404)
    if request.method != "POST":
        return JsonResponse({"ok": True})
    if not shopify.verify_webhook(sup, request.body, request.headers.get("X-Shopify-Hmac-Sha256", "")):
        return JsonResponse({"error": "signature invalide"}, status=401)
    try:
        data = _json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"error": "JSON invalide"}, status=400)
    try:
        code, msg = shopify.handle_webhook(sup, request.headers.get("X-Shopify-Topic", ""), data)
    except Exception:
        logger.exception("Webhook Shopify")
        code, msg = 200, "erreur interne journalisee"   # 200 : evite les relances en boucle cote Shopify
    return JsonResponse({"message": msg}, status=200 if code in (200, 404) else code)


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
            elif ext in ("tiff", "tif") or "_poster" in low or "_supplier.json" in low or \
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
                     "_colors.json": "Couleurs (JSON)", "_supplier.json": "Fiche fournisseur (sans prix)",
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
                if request.POST.get("ship") and o.status in (Order.PAID, Order.FULFILLED):
                    o.status = Order.SHIPPED; o._erp_user = user; fields += ["status", "status_changed_at"]
                o.save(update_fields=fields)
                log("action", "Suivi : %s %s" % (o.carrier, o.tracking_number))
                messages.success(request, "Suivi enregistré.")
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
    return render(request, "admin/erp_order.html", {"paid_statuses": Order.PAID_STATUSES, "fin": fin,
        **_admin.site.each_context(request), "o": o, "files": files, "visuals": visuals, "have": have, "sfiles": supplier_files_status(o.uid),
        "sfiles_ok": all(f["ok"] for f in supplier_files_status(o.uid)),
        "has_tiff": any(f["ext"] in ("TIFF", "TIF") for _k, _l, fs in files for f in fs),
        "has_json": any(f["name"].endswith("_order.json") for _k, _l, fs in files for f in fs),
        "events": o.events.all()[:60], "flags": flags, "others": others, "ad": ad,
        "statuses": Order.STATUS_CHOICES, "suppliers": Supplier.objects.all(),
        "age": (timezone.now() - (o.status_changed_at or o.created_at)).days,
        "erp_section": "orders"})


# ---------------- Liste des commandes (ERP) ----------------
ORDER_TABS = [("todo", "À traiter"), ("pending", "En attente"), ("paid", "Payées"), ("fulfilled", "En production"),
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
        elif op in ("shipped", "delivered", "failed", "fulfilled"):
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
               (o.status == Order.FULFILLED and age.days >= lead) or \
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
        messages.success(request, "Informations légales enregistrées : pages publiques et factures à jour.")
        return redirect("studio:erp_legal")
    sections = [
        ("Entreprise", "Apparaît dans les mentions légales, les CGV et sur chaque facture.",
         ["legal_name", "legal_form", "capital", "siret", "rcs", "address", "director"]),
        ("Contact", "Adresse affichée aux clients pour le SAV et les réclamations.", ["email", "phone"]),
        ("TVA et factures", "Taux 0 = micro-entreprise en franchise (mention art. 293 B du CGI sur la facture).",
         ["vat_rate", "vat_number", "invoice_prefix"]),
        ("Banque", "Compte débité pour les virements fournisseurs (ordres SEPA générés par l'ERP).", ["bank_iban", "bank_bic"]),
        ("Fabrication", "Zone de fabrication réelle : utilisée uniquement pour la politique de confidentialité (transferts de données). Le site affiche « Fabriqué dans nos ateliers ».",
         ["production_origin"]),
        ("Hébergeur et médiation", "Obligatoires : nom, adresse et téléphone de l'hébergeur, médiateur de la consommation.",
         ["host", "mediator"]),
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
    if o.status not in (Order.SHIPPED, Order.DELIVERED, Order.FULFILLED):
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
        **_admin.site.each_context(request), "erp_section": "compta", "m": start.strftime("%Y-%m"), "month": start,
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
