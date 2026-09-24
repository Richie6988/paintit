import os
import logging
logger = logging.getLogger("studio.views")
import threading
import uuid

from django.conf import settings
from django.utils import timezone
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
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
    "read":   {"fr": "Lecture de l'image", "en": "Reading the image", "de": "Bild wird gelesen", "es": "Leyendo la imagen"},
    "colors": {"fr": "Analyse des couleurs", "en": "Analyzing colors", "de": "Farbanalyse", "es": "Analizando colores"},
    "zones":  {"fr": "Regroupement des zones", "en": "Grouping zones", "de": "Zonen werden gruppiert", "es": "Agrupando zonas"},
    "canvas": {"fr": "Trace du canvas", "en": "Drawing the canvas", "de": "Canvas wird gezeichnet", "es": "Trazando el lienzo"},
    "number": {"fr": "Numerotation des zones", "en": "Numbering zones", "de": "Zonen werden nummeriert", "es": "Numerando zonas"},
    "final":  {"fr": "Finalisation", "en": "Finishing up", "de": "Abschluss", "es": "Finalizacion"},
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
    return render(request, "studio/home.html",
                  {"library_preview": lib, "showcase_slides": _json.dumps(slides),
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
    return path


def _run_generation(uid, src, colors, w, h, fmt, orientation, lang, focus=(0.5, 0.5), source_name=None):
    def cb(pct, label):
        jobs.update(uid, pct=int(pct), label=label)

    try:
        result = generate(src, colors, w, h, uid=uid, progress=cb, focus=focus, source_name=source_name)
        canvas_price = compute_price(fmt, colors)
        order = {
            **result, "colors": colors, "width_cm": w, "height_cm": h,
            "orientation": orientation, "format_label": FORMATS[fmt][2],
            "difficulty": "auto", "lang": lang,
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
                orientation = form.cleaned_data["orientation"]
                w, h = dimensions(fmt, orientation)
                try:
                    fx = float(request.POST.get("focus_x", "0.5"))
                    fy = float(request.POST.get("focus_y", "0.5"))
                except (TypeError, ValueError):
                    fx, fy = 0.5, 0.5
                uid = f"{uuid.uuid4().hex[:8].upper()}-{uuid.uuid4().hex[:4].upper()}"
                jobs.start(uid)
                sname = request.session.get("last_photo_name") or os.path.basename(src)
                try:
                    genqueue.submit(_run_generation, uid, src, colors, w, h, fmt,
                                    orientation, get_language() or "fr", (fx, fy),
                                    source_name=sname)
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
                       "colors": str(order.get("colors"))}
        form = UploadForm(initial=initial)
    return render(request, "studio/upload.html", {
        "form": form, "price_cfg": price_cfg(),
        "has_photo": bool(last_photo and os.path.exists(last_photo)),
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
        _save_digital(); _track()
        request.session["free_used"] = True
        return redirect("studio:digipaint", uid=uid)   # toile numerique -> le jeu

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
    canvas = compute_price("40x50", colors if colors in (12, 24, 36) else 24)
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
    back_url = reverse("studio:preview") if (order and order.get("uid") == uid) else reverse("studio:paint_portal")
    return render(request, "studio/digipaint.html",
                  {"uid": uid, "palette": palette, "owned": owned, "back_url": back_url})


def preview(request):
    order = request.session.get("order")
    if not order:
        return redirect("studio:upload")
    p = Pricing.get()
    from .models import KitFormat
    colors = order.get("colors", 24)
    formats = [{"id": kf.id, "label": kf.label, "w": kf.width_cm, "h": kf.height_cm,
                "price": round(float(kf.price) + p.color_price(colors), 2)}
               for kf in KitFormat.objects.filter(available=True)]
    current_format = next((fm["id"] for fm in formats
                           if fm["w"] == order.get("width_cm") and fm["h"] == order.get("height_cm")),
                          (formats[0]["id"] if formats else None))
    # aligne le prix affiche sur le format courant
    if formats:
        cf = next((fm for fm in formats if fm["id"] == current_format), formats[0])
        order["canvas_price"] = cf["price"]
        order["width_cm"], order["height_cm"], order["format_label"] = cf["w"], cf["h"], cf["label"]
        order["price"] = round(cf["price"] + order.get("brushes_amount", 0.0), 2)
        request.session["order"] = order
    return render(request, "studio/preview.html",
                  {"order": order, "brushes_price": p.brushes_price, "formats": formats,
                   "current_format": current_format,
                   "brushes_available": p.av_brushes})


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
                order["width_cm"] = kf.width_cm
                order["height_cm"] = kf.height_cm
                order["format_label"] = kf.label
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
    path = os.path.join(settings.MEDIA_ROOT, "orders", uid, name)
    if not os.path.isfile(path):
        raise Http404
    resp = FileResponse(open(path, "rb"), as_attachment=not name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".pdf")))
    resp["X-Robots-Tag"] = "noindex, nofollow"
    resp["Cache-Control"] = "private, no-store"
    return resp


def restore_model(request):
    """Reprise d'un modele sauvegarde (Mes creations) : reconstitue la commande en session."""
    if request.method != "POST":
        return redirect("studio:home")
    uid = request.POST.get("uid", "")
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
    if not uid or not os.path.isdir(d):
        return redirect("studio:my_models")
    import json
    cj = os.path.join(d, f"{uid}_colors.json")
    colors_list = json.load(open(cj, encoding="utf-8")) if os.path.exists(cj) else []
    try:
        w = float(request.POST["width_cm"]); h = float(request.POST["height_cm"])
        colors = int(request.POST["colors"])
    except (KeyError, TypeError, ValueError):
        return redirect("studio:my_models")
    canvas = compute_price("40x50", colors)
    request.session["order"] = {
        "uid": uid, "colors": colors, "width_cm": w, "height_cm": h,
        "orientation": request.POST.get("orientation", "portrait"),
        "format_label": request.POST.get("format_label", "40 x 50 cm"),
        "difficulty": "auto", "lang": get_language() or "fr",
        "canvas_price": canvas, "brushes": False, "brushes_amount": 0.0,
        "price": canvas, "colors_list": colors_list,
    }
    return redirect("studio:preview")


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


def gallery(request):
    """Onglet Gallery : toutes les toiles PaintIt en vrac, filtrables par recherche."""
    models = list(DigitalCanvas.objects.filter(email="__library__", uid__startswith="gal-").order_by("category", "title"))
    return render(request, "studio/gallery.html", {"models": models})


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
    return JsonResponse({"ok": True, "redirect": ("/gallery/buy/%s/" % _bp) if _bp else "/paint/"})


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
    # Toiles suivantes : parcours normal (jeu gratuit 5 min, sauvegarde a 0,99 EUR via le mur de paiement).
    return redirect("studio:upload")


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
                                "colors": colors if colors in (12, 24, 36) else 24}
    request.session["buy_mode"] = True
    return redirect("studio:upload")


def my_models_email(request):
    if request.method != "POST":
        return redirect("studio:my_models")
    import json
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError
    email = (request.POST.get("email") or "").strip()
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({"error": "email"}, status=400)
    try:
        models = json.loads(request.POST.get("models", "[]"))[:60]
    except Exception:
        models = []
    emails.send_gallery(email, models, get_language() or "fr")
    return JsonResponse({"ok": True})


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
        "Disallow: /create/\nDisallow: /preview/\nDisallow: /media/\n"
        "Disallow: /delivery/\nDisallow: /checkout/\n"
        "Allow: /llm.txt\nAllow: /llm.json\nAllow: /gallery/\n"
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
            generate(src, o.get("colors", 24), o.get("width_cm", 40), o.get("height_cm", 50),
                     uid=uid, source_name=os.path.basename(src))
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
        "price": o["price"], "cost": supplier.estimate_cost(o),
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


def llm_txt(request):
    from django.http import HttpResponse
    site = settings.SITE_URL
    body = """# PaintIt , la peinture par numeros reinventee (a partir de vos photos)

PaintIt transforme n'importe quelle photo en une toile a peindre par numeros, personnalisee,
d'une precision epoustouflante. Apercu gratuit en moins d'une minute. Toile physique livree chez
vous + une toile numerique jouable en ligne offerte.

## Mots-cles
peinture par numeros personnalisee, toile a peindre a partir d'une photo, paint by numbers custom,
peinture par numero adulte, cadeau personnalise photo, tableau a peindre soi-meme, kit peinture
par numeros, toile numerotee, peinture par numeros sur mesure, transformer photo en peinture,
custom paint by numbers from photo, peinture par numeros enfant, idee cadeau original, deco murale
personnalisee, loisir creatif anti-stress, peinture numerique en ligne.

## Proposition de valeur
- Le meilleur algorithme de generation de peinture par numeros du marche (rendu net, zones propres).
- Apercu genere GRATUITEMENT en moins d'une minute a partir de votre image.
- Kit physique livre : toile numerotee, pots de peinture assortis, pinceaux, emballage soigne.
- Bonus digital : une toile numerique jouable en ligne (peindre, score, recompenses).
- Formats multiples (30x40, 40x40, 40x50...) et nombre de couleurs ajustable.
- Livraison offerte.

## Cas d'usage
- Cadeau personnalise (anniversaire, mariage, naissance, Noel, fete des meres/peres).
- Portrait de famille, animal de compagnie, paysage, souvenir de voyage.
- Activite relaxante / anti-stress, loisir creatif adulte et enfant.
- Deco murale unique a partir d'une photo qui compte.

## FAQ (questions frequentes)
Q: Comment transformer une photo en peinture par numeros ? R: Envoyez votre photo sur PaintIt, l'algorithme genere une toile numerotee personnalisee en moins d'une minute.
Q: Est-ce gratuit ? R: L'apercu est gratuit et une toile numerique est offerte.
Q: Quels formats ? R: Plusieurs formats (30x40, 40x40, 40x50 cm et plus), avec un nombre de couleurs ajustable.
Q: Que contient le kit ? R: Une toile numerotee, des pots de peinture assortis, des pinceaux, un poster/instructions, et la toile numerique jouable.
Q: Peut-on peindre en ligne ? R: Oui, chaque creation donne une toile numerique jouable (mode digital) avec score.
Q: Faut-il savoir dessiner ? R: Non, il suffit de peindre les zones numerotees avec les bonnes couleurs.
Q: Combien de temps pour recevoir ? R: Livraison offerte, delais indiques au checkout.
Q: Bon cadeau ? R: Oui, ideal comme cadeau personnalise a partir d'une photo qui compte.
Q: Peut-on commander une impression finie ? R: Oui, l'option \"Tableau fini\" imprime votre oeuvre, prete a accrocher.

## Liens
- Accueil: %(s)s/
- Creer votre toile (gratuit): %(s)s/create/
- Galerie de modeles: %(s)s/gallery/
- Mes toiles / jeu digital: %(s)s/paint/
- Contact: %(s)s/contact/
- Confidentialite: %(s)s/privacy/
- Sitemap: %(s)s/sitemap.xml
""" % {"s": site}
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


def llm_json(request):
    from django.http import JsonResponse
    site = settings.SITE_URL
    data = {
        "name": "PaintIt",
        "tagline": "La peinture par numeros reinventee, a partir de vos photos.",
        "description": "PaintIt transforme n'importe quelle photo en toile a peindre par numeros "
                       "personnalisee, livree chez vous, avec une toile numerique jouable offerte.",
        "keywords": ["peinture par numeros personnalisee", "toile a peindre a partir d'une photo",
                     "custom paint by numbers from photo", "cadeau personnalise photo",
                     "peinture par numero adulte", "kit peinture par numeros", "toile numerotee",
                     "transformer photo en peinture", "deco murale personnalisee",
                     "loisir creatif anti-stress", "peinture numerique en ligne"],
        "value_props": ["Meilleur algorithme du marche", "Apercu gratuit en moins d'une minute",
                        "Kit physique livre (toile + peinture + pinceaux)",
                        "Toile numerique jouable offerte", "Livraison offerte",
                        "Formats et couleurs ajustables"],
        "use_cases": ["cadeau personnalise", "portrait de famille", "animal de compagnie",
                      "souvenir de voyage", "deco murale", "activite anti-stress"],
        "faq": [
            {"q": "Comment transformer une photo en peinture par numeros ?",
             "a": "Envoyez votre photo, l'algorithme genere une toile numerotee en moins d'une minute."},
            {"q": "Est-ce gratuit ?", "a": "L'apercu est gratuit et une toile numerique est offerte."},
            {"q": "Quels formats ?", "a": "30x40, 40x40, 40x50 cm et plus, couleurs ajustables."},
            {"q": "Que contient le kit ?", "a": "Toile numerotee, pots de peinture, pinceaux, poster, toile numerique."},
            {"q": "Faut-il savoir dessiner ?", "a": "Non, il suffit de peindre les zones numerotees."},
            {"q": "Peut-on peindre en ligne ?", "a": "Oui, chaque creation donne une toile numerique jouable."},
        ],
        "links": {"home": site + "/", "create": site + "/create/", "gallery": site + "/gallery/",
                  "paint": site + "/paint/", "contact": site + "/contact/",
                  "privacy": site + "/privacy/", "sitemap": site + "/sitemap.xml"},
    }
    return JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})


def sitemap_xml(request):
    from django.http import HttpResponse
    from django.urls import translate_url
    from .models import DigitalCanvas
    site = settings.SITE_URL
    # pages publiques uniquement (les toiles /paint/<uid>/ ne sont pas indexees)
    paths = ["/", "/create/", "/gallery/", "/paint/", "/contact/", "/privacy/"]
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
    return HttpResponse("\n".join(x), content_type="application/xml")


def shop_pricing(request):
    from django.contrib.admin.views.decorators import staff_member_required
    from django.contrib import messages
    from .models import PrintPricing
    if not request.user.is_staff:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.get_full_path())
    kit = Pricing.get(); tab = PrintPricing.get()
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
        kit.c_12 = f("c_12"); kit.c_24 = f("c_24"); kit.c_36 = f("c_36")
        kit.av_c12 = bb("av_c12"); kit.av_c24 = bb("av_c24"); kit.av_c36 = bb("av_c36")
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
    from .models import KitFormat
    ctx = {
        "kit": kit,
        "kit_formats": KitFormat.objects.all(),
        "kit_colors": [{"key":"12","price":kit.c_12,"av":kit.av_c12},
                       {"key":"24","price":kit.c_24,"av":kit.av_c24},
                       {"key":"36","price":kit.c_36,"av":kit.av_c36}],
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
    ctx = {"results": None, "error": None, "erp_section": "marketing",
           "link": request.POST.get("link", "https://paintit.click/create/"),
           "colors": request.POST.get("colors", "24"),
           "campaign": request.POST.get("campaign", ""),
           "variant": request.POST.get("variant", "")}
    texts, gif = ({}, {})
    base = None
    if request.method == "POST":
        texts, gif = _mkt_form_texts(request, marketing)
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
            ctx.update(uid=base.image_uid, campaign=base.campaign, link=base.target_url)
            ctx["results"] = ads     # apercus de la variante dupliquee
    ctx["promos"] = [{"key": k, "label": lbl, "slots": marketing.template_slots(k, texts.get(k))}
                     for k, lbl in marketing.TEMPLATES]
    ctx["gif_slots"] = marketing.gif_slots(gif)
    if request.method == "POST":
        f = request.FILES.get("image")
        link = (request.POST.get("link") or "").strip() or settings.SITE_URL + "/create/"
        colors = int(request.POST.get("colors") or 24)
        reuse = (request.POST.get("reuse_uid") or "").strip()
        reuse_ok = bool(reuse) and bool(_re.match(r"^mkt-[A-Za-z0-9]+$", reuse)) and \
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
                produced = marketing.build_all(uid, texts=texts, gif_texts=gif, links=links, suffix=suffix)
                campaign = (request.POST.get("campaign") or "").strip()[:80]
                for key, label, path, kind in produced:
                    MarketingAd.objects.create(
                        token=tokens[key], image_uid=uid, campaign=campaign, variant=variant, kind=key,
                        label=label, fmt="gif" if kind == "gif" else "web", file_name=os.path.basename(path),
                        target_url=link, texts=gif if kind == "gif" else
                        dict(marketing.template_defaults(key), **texts.get(key, {})))
                ctx.update(uid=uid, variant=variant, campaign=campaign)
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
        "erp_section": "marketing"})


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


def _sup_card(sup):
    from . import erp
    sup.stats = erp.supplier_stats(sup)
    sup.color = _sup_color(sup.name)
    sup.initials = "".join(w[0] for w in (sup.name or "?").split()[:2]).upper()
    sup.channel = ("API" if sup.integration == "api" and sup.api_url else
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
        elif op == "assign":
            n = 0
            for o in erp.unassigned_orders():
                Order.objects.filter(pk=o.pk).update(supplier=sup); n += 1
            messages.success(request, "%d commande(s) rattachee(s) a %s." % (n, sup.name))
        return redirect(request.get_full_path())
    _sup_card(sup)
    tab = request.GET.get("tab", "open")
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
    automation = {"callback": integrations.callback_url(sup), "retries": len([x for x in integrations.pending_retries()
                                                                              if x.supplier_id == sup.pk]),
                  "out": _json.dumps(sample_out, ensure_ascii=False, indent=2),
                  "in": _json.dumps(sample_in, ensure_ascii=False, indent=2)}
    return render(request, "admin/erp_supplier.html", {
        "automation": automation,
        **_admin.site.each_context(request), "sup": sup, "tab": tab, "rows": rows,
        "tabs": [(k, l, q.count()) for k, l, q in tabs], "events": events,
        "iban": mask(sup.iban), "api_key": mask(sup.api_key, 3),
        "files": [lbl for flag, lbl in [("want_template_pdf", "Toile numérotée PDF vectoriel"),
                                        ("want_source", "Photo source"), ("want_template_svg", "Toile numérotée .svg"),
                                        ("want_template_tiff", "Toile numérotée .tiff"),
                                        ("want_preview_svg", "Aperçu colorié .svg"), ("want_poster", "Poster PNG"),
                                        ("want_order_json", "Fiche JSON (sans prix)")] if getattr(sup, flag, False)],
        "unassigned": erp.unassigned_orders().count(), "erp_section": "suppliers"})


def _supplier_form_class():
    from django import forms
    from .models import Supplier

    class SupplierForm(forms.ModelForm):
        class Meta:
            model = Supplier
            exclude = ("created_at", "last_sync_at", "last_sync_ok", "last_sync_error", "webhook_secret")
            widgets = {k: forms.Textarea(attrs={"rows": 3}) for k in
                       ("address", "pricing", "terms", "production_notes", "notes")}
    return SupplierForm


@staff_member_required
def erp_supplier_edit(request, pk=None):
    from django.contrib import admin as _admin
    from django.contrib import messages
    from .models import Supplier
    sup = Supplier.objects.filter(pk=pk).first() if pk else None
    if pk and not sup:
        raise Http404
    Form = _supplier_form_class()
    if request.method == "POST" and request.POST.get("op") == "delete" and sup:
        name = sup.name
        sup.delete()
        messages.success(request, "Fournisseur %s supprime." % name)
        return redirect("studio:erp_suppliers")
    form = Form(request.POST or None, instance=sup)
    if request.method == "POST" and form.is_valid():
        sup = form.save()
        messages.success(request, "Fournisseur enregistre.")
        return redirect("studio:erp_supplier", pk=sup.pk)
    f = form
    sections = [
        ("Identité", "Nom, statut et checkout servi par ce fournisseur.", ["name", "active", "checkout", "priority"]),
        ("Intégration", "Comment les commandes lui sont transmises : e-mail (liens des fichiers) ou API (POST JSON, Bearer).",
         ["integration", "auto_dispatch", "email", "api_url", "api_key"]),
        ("Fichiers transmis", "Fichiers joints à chaque commande.",
         ["want_template_pdf", "want_template_tiff", "want_template_svg", "want_poster", "want_order_json",
          "want_source", "want_preview_svg"]),
        ("Contact", "", ["contact_name", "contact_email", "phone", "address"]),
        ("Production & conditions", "", ["lead_time_days", "incoterms", "production_notes", "pricing", "terms"]),
        ("Banque", "Coordonnées de paiement du fournisseur.", ["bank_name", "iban", "bic"]),
        ("Notes internes", "", ["notes"]),
    ]
    sec = [{"title": t, "help": h, "fields": [f[n] for n in names if n in f.fields]} for t, h, names in sections]
    return render(request, "admin/erp_supplier_form.html", {
        **_admin.site.each_context(request), "form": form, "sup": sup, "sections": sec,
        "erp_section": "suppliers"})


# ---------------- Catalogue (galerie de modeles + toiles clients) ----------------
LIB_EMAIL = "__library__"


def _slug(text):
    import re as _re, unicodedata
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return _re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40] or "modele"


def _create_gallery_model(title, category="", price=0, colors=24, orientation="portrait",
                          image_path=None, from_uid=None, in_slider=False):
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
                   in_slider=in_slider)
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
                                              in_slider=bool(request.POST.get("in_slider")))
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
                                          image_path=tmp, in_slider=bool(request.POST.get("in_slider")))
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
    return render(request, "admin/erp_order.html", {
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
