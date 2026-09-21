import os
import logging
logger = logging.getLogger("studio.views")
import threading
import uuid

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.utils.translation import get_language
from django.contrib.admin.views.decorators import staff_member_required

from . import fulfillment, supplier, discounts, address, emails, payments, receipts, jobs
from .forms import UploadForm, DeliveryForm, ContactForm, FORMATS, dimensions
from .models import Order, Pricing, DigitalCanvas, EmailCode
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
    lib = list(DigitalCanvas.objects.filter(email="__library__", uid__startswith="gal-").order_by("?")[:12])
    showdir = os.path.join(settings.BASE_DIR, "studio", "static", "studio", "showcase")
    slugs = []
    if os.path.isdir(showdir):
        for f in sorted(os.listdir(showdir)):
            if f.endswith("_pbn.png"):
                sg = f[:-8]
                if os.path.exists(os.path.join(showdir, sg + "_template.png")):
                    slugs.append(sg)
    return render(request, "studio/home.html",
                  {"library_preview": lib, "showcase_slugs": _json.dumps(slugs)})


# ---------------- Upload / preview ----------------
def _save_upload(f):
    ext = os.path.splitext(f.name)[1].lower() or ".jpg"
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
                threading.Thread(target=_run_generation, daemon=True,
                                 args=(uid, src, colors, w, h, fmt, orientation,
                                       get_language() or "fr", (fx, fy)),
                                 kwargs={"source_name": sname}).start()
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
    d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
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
    return render(request, "studio/digipaint.html",
                  {"uid": uid, "palette": palette, "owned": owned})


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
    digital_owned = DigitalCanvas.objects.filter(uid=order["uid"]).exists() or not request.session.get("free_used")
    return render(request, "studio/preview.html",
                  {"order": order, "brushes_price": p.brushes_price, "formats": formats,
                   "current_format": current_format,
                   "brushes_available": p.av_brushes, "digital_owned": digital_owned})


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
    import random
    code = "%06d" % random.randint(0, 999999)
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
    ok = EmailCode.objects.filter(email=email, code=code,
                                  created_at__gte=timezone.now() - timedelta(minutes=15)).exists()
    if not ok:
        return JsonResponse({"error": "code"}, status=400)
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
    return redirect("studio:paint_pay")


def paint_pay(request):
    # Toile supplementaire : 0,99 EUR (demo -> credit immediat ;
    # avec Stripe, brancher ici un Checkout dedie).
    request.session["paid_credits"] = request.session.get("paid_credits", 0) + 1
    return redirect("studio:paint_new")


def paint_unlock(request, uid):
    """Essai gratuit : on entre directement dans le jeu (2 min), le paiement se fait ensuite
    via le mur 'pour continuer' (paint_confirm)."""
    return redirect("studio:digipaint", uid=uid)


def _credit_digital(request, uid):
    """Enregistre/deverrouille la toile numerique dans la galerie de l'utilisateur."""
    verified = request.session.get("verified_email", "")
    o = request.session.get("order") or {}
    import os, json as _json
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
        pass
    request.session["free_used"] = True
    if verified:
        uids = request.session.get("my_uids", [])
        if uid not in uids: uids.append(uid); request.session["my_uids"] = uids[-60:]


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
    # Demo : pas de paiement reel -> credit immediat.
    _credit_digital(request, uid)
    return JsonResponse({"ok": True})


def paint_unlock_success(request):
    """Retour Stripe apres paiement de la toile numerique : credite puis ouvre le jeu."""
    uid = request.GET.get("uid", "")
    sid = request.GET.get("sid", "")
    if uid and sid and payments.stripe_live():
        paid, meta_uid = payments.session_is_paid(sid)
        if paid and (meta_uid == uid or not meta_uid):
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
    if request.method != "POST":
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
    threading.Thread(target=_run_game_generation, daemon=True,
                     args=(gameuid, src, colors, w_cm, h_cm, detail),
                     kwargs={"source_name": os.path.basename(src), "max_zones": max_zones,
                             "min_zone_mm": min_zone_mm, "density": density}).start()
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


def _notify_supplier(o, shipping):
    """Plug & play : route la commande vers le fournisseur connecte au checkout
    (e-mail avec liens fichiers, ou POST JSON API). Ne casse jamais la commande."""
    try:
        from .models import Supplier
        sup = Supplier.for_checkout("kit")
    except Exception:
        logger.exception("Lecture fournisseur (migration manquante ?)")
        return
    if not sup:
        return
    uid = o.get("uid")
    base = settings.SITE_URL + settings.MEDIA_URL + "orders/%s/" % uid
    files = {name: base + name for name in sup.wanted_files(uid)}
    if getattr(sup, "want_source", False):
        import glob as _glob
        d = os.path.join(settings.MEDIA_ROOT, "orders", uid)
        for sp in _glob.glob(os.path.join(d, "%s_source_*" % uid)):
            fn = os.path.basename(sp); files[fn] = base + fn; break
    if sup.integration == "api" and sup.api_url:
        try:
            import json as _json, urllib.request
            payload = _json.dumps({
                "order": {"uid": uid, "format": o.get("format_label"), "colors": o.get("colors"),
                          "width_cm": o.get("width_cm"), "height_cm": o.get("height_cm"),
                          "total": o.get("total")},
                "shipping": shipping, "files": files}).encode("utf-8")
            req = urllib.request.Request(sup.api_url, data=payload, headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + (sup.api_key or "")})
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            logger.exception("API fournisseur %s (%s)", sup.name, uid)
    elif sup.email:
        try:
            from django.core.mail import EmailMessage
            body = ("Nouvelle commande %s\n\nFormat : %s (%sx%s cm), %s couleurs\n\n"
                    "Fichiers a imprimer :\n%s\n\nLivraison :\n%s\n%s\n%s %s (%s)\nTel : %s"
                    % (uid, o.get("format_label"), o.get("width_cm"), o.get("height_cm"), o.get("colors"),
                       "\n".join(files.values()),
                       shipping.get("full_name", ""), shipping.get("address1", ""),
                       shipping.get("postal_code", ""), shipping.get("city", ""),
                       shipping.get("country", ""), shipping.get("phone", "")))
            EmailMessage("PaintIt , commande %s" % uid, body,
                         settings.DEFAULT_FROM_EMAIL, [sup.email]).send(fail_silently=True)
        except Exception:
            logger.exception("Mail fournisseur %s (%s)", sup.name, uid)


def _safe_export_tiff(uid):
    try:
        from .pipeline import export_tiff
        export_tiff(uid)
    except Exception:
        logger.exception("Export TIFF apercu %s", uid)


def _reformat_for_supplier(o):
    """Apres paiement, avant l'envoi fournisseur : regenere la toile aux dimensions
    du format commande, puis exporte les .tiff (sans perte). Conserve .svg/.png."""
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
    try:
        export_tiff(uid)
    except Exception:
        logger.exception("Export TIFF %s", uid)


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
    discounts.issue_referral(o["uid"] + "-R")
    _upsert_order(o, shipping, status=Order.FULFILLED, supplier_ref=supplier_result.get("supplier_ref"))
    # Lourd (regen toile au bon format + TIFF + notif fournisseur + email) -> tache de fond,
    # APRES paiement, pour repondre tout de suite (pas de lag au checkout).
    threading.Thread(target=_post_order_async, args=(dict(o), dict(shipping)), daemon=True).start()
    return o, manifest, supplier_result


def _post_order_async(o, shipping):
    """Traitement post-paiement en arriere-plan : regen aux dimensions, TIFF, fournisseur, email."""
    try:
        _reformat_for_supplier(o)
    except Exception:
        logger.exception("Reformat async %s", o.get("uid"))
    try:
        _notify_supplier(o, shipping)
    except Exception:
        logger.exception("Notify async %s", o.get("uid"))
    try:
        emails.send_order_confirmation(o, shipping)
    except Exception:
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
    o = dict(order); o["discount"] = discount; o["total"] = total
    o["lang"] = (get_language() or o.get("lang", "fr"))[:2]   # langue du process (achat)

    if payments.stripe_live():
        _upsert_order(o, shipping, status=Order.PENDING)
        try:
            url = payments.create_checkout_session(o, shipping, request)
        except Exception as exc:
            return render(request, "studio/checkout.html", {
                "order": order, "shipping": shipping, "discount": discount, "total": total,
                "stripe": True, "msg": {"kind": "err", "text": f"Stripe indisponible : {exc}"}})
        return redirect(url)

    # Demo : pas de paiement reel -> on honore la commande maintenant.
    o, manifest, supplier_result = _fulfill(o, shipping)
    request.session.pop("applied_discount", None)
    _record_gift(o, shipping)
    request.session["verified_email"] = shipping.get("email", "")   # galerie auto apres achat
    _rc = (o["uid"] + "-R").upper()
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


def _ensure_fulfilled(uid):
    try:
        r = Order.objects.get(uid=uid)
    except Order.DoesNotExist:
        return None
    if r.status != Order.FULFILLED:
        _fulfill(_order_from_row(r), _shipping_from_row(r))
        r.refresh_from_db()
    return r


def pay_success(request):
    uid = request.GET.get("uid")
    r = _ensure_fulfilled(uid) if uid else None
    if not r:
        return redirect("studio:home")
    request.session.pop("applied_discount", None)
    o = _order_from_row(r)
    ship = _shipping_from_row(r)
    _record_gift(o, ship)
    request.session["verified_email"] = ship.get("email", "")   # galerie auto apres achat
    _rc = (o["uid"] + "-R").upper()
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
    if event.get("type") == "checkout.session.completed":
        uid = (event["data"]["object"].get("metadata", {}) or {}).get("uid") \
              or event["data"]["object"].get("client_reference_id")
        if uid:
            _ensure_fulfilled(uid)
    return HttpResponse(status=200)


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
    return render(request, "studio/privacy.html")


def contact(request):
    sent = False
    attach_error = None
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
    from collections import OrderedDict
    from .models import ContactMessage
    rows = Order.objects.filter(status=Order.FULFILLED).order_by("created_at")
    by_month = OrderedDict()
    for o in rows:
        m = o.created_at.strftime("%Y-%m")
        b = by_month.setdefault(m, {"t": 0.0, "c": 0.0, "n": 0})
        b["t"] += o.total; b["c"] += o.cost; b["n"] += 1
    months = list(by_month)
    turnover = [round(by_month[m]["t"], 2) for m in months]
    cost = [round(by_month[m]["c"], 2) for m in months]
    benefit = [round(t - c, 2) for t, c in zip(turnover, cost)]
    counts = [by_month[m]["n"] for m in months]
    kpis = {"orders": rows.count(),
            "turnover": round(sum(turnover), 2), "cost": round(sum(cost), 2),
            "benefit": round(sum(benefit), 2),
            "pending": Order.objects.filter(status=Order.PENDING).count(),
            "unanswered": ContactMessage.objects.filter(answered=False).count()}
    from .models import Discount
    kpis["discounts_used"] = Discount.objects.filter(status="used").count()
    kpis["discounts_issued"] = Discount.objects.filter(status="issued").count()
    recent = Order.objects.order_by("-created_at")[:12]
    fmt = OrderedDict()
    for o in rows:
        fb = fmt.setdefault(o.format_label, {"n": 0, "t": 0.0, "b": 0.0})
        fb["n"] += 1; fb["t"] += o.total; fb["b"] += o.benefit
    per_format = [{"label": k, "n": v["n"], "t": round(v["t"], 2), "b": round(v["b"], 2)}
                  for k, v in sorted(fmt.items(), key=lambda kv: -kv[1]["t"])]
    return render(request, "studio/finance.html", {
        "months": months, "turnover": turnover, "cost": cost, "benefit": benefit,
        "counts": counts, "kpis": kpis, "recent": recent, "per_format": per_format})

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
    paths = ["/", "/create/", "/gallery/", "/paint/", "/contact/", "/privacy/"]
    # modeles de galerie indexables
    for uid in DigitalCanvas.objects.filter(email="__library__", uid__startswith="gal-").values_list("uid", flat=True):
        paths.append("/paint/%s/" % uid)
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


@staff_member_required
def marketing_page(request):
    from . import marketing
    KEYS = ["slider", "shiny", "zoom"]
    ctx = {"results": None, "error": None,
           "cta": request.POST.get("cta", "Testez notre algorithme"),
           "brand": request.POST.get("brand", "PaintIt"),
           "link": request.POST.get("link", "https://paintit.click"),
           "colors": request.POST.get("colors", "24"),
           "gif_msg": request.POST.get("msg_gif", ""),
           "labels": dict(marketing.TEMPLATES),
           "defaults": marketing.DEFAULT_MSG,
           "keys": KEYS}
    # valeurs des messages par promo (pre-remplies avec les defauts)
    labels = dict(marketing.TEMPLATES)
    ctx["promos"] = [{"key": k, "label": labels.get(k, k),
                      "msg": (request.POST.get("msg_" + k) if request.method == "POST"
                              else marketing.DEFAULT_MSG.get(k, "")),
                      "sub": (request.POST.get("sub_" + k) if request.method == "POST"
                              else marketing.DEFAULT_SUB.get(k, ""))} for k in KEYS]
    if request.method == "POST":
        f = request.FILES.get("image")
        cta = (request.POST.get("cta") or "Testez notre algorithme").strip()
        brand = (request.POST.get("brand") or "PaintIt").strip()
        link = (request.POST.get("link") or "").strip()
        colors = int(request.POST.get("colors") or 24)
        messages = {k: (request.POST.get("msg_" + k) or marketing.DEFAULT_MSG.get(k, "")).strip() for k in KEYS}
        messages["gif"] = (request.POST.get("msg_gif") or "").strip()
        messages["gif_sub"] = cta
        subs = {k: (request.POST.get("sub_" + k) if request.POST.get("sub_" + k) is not None
                    else marketing.DEFAULT_SUB.get(k, "")) for k in KEYS}
        import re as _re, uuid as _uuid
        reuse = (request.POST.get("reuse_uid") or "").strip()
        reuse_ok = bool(reuse) and bool(_re.match(r"^mkt-[A-Za-z0-9]+$", reuse)) and             os.path.exists(os.path.join(settings.MEDIA_ROOT, "orders", reuse, "%s_preview.svg" % reuse))
        if not f and not reuse_ok:
            ctx["error"] = "Ajoutez une image."
        else:
            try:
                if reuse_ok and not f:
                    uid = reuse                 # regenere avec la meme image, textes modifies
                else:
                    uid = "mkt-" + _uuid.uuid4().hex[:10]
                    tmp = os.path.join(settings.MEDIA_ROOT, "uploads", uid + "_in.jpg")
                    os.makedirs(os.path.dirname(tmp), exist_ok=True)
                    with open(tmp, "wb") as out:
                        for chunk in f.chunks():
                            out.write(chunk)
                    generate(tmp, colors, 40, 50, uid=uid, source_name=f.name)
                produced = marketing.build_all(uid, cta=cta, messages=messages, subs=subs, brand=brand, link=link)
                ctx["uid"] = uid
                ctx["results"] = [{"label": lbl,
                                   "url": "/marketing/file/%s/%s/" % (uid, os.path.basename(path)),
                                   "is_gif": (kind == "gif")} for lbl, path, kind in produced]
            except Exception as exc:
                logger.exception("Marketing build")
                ctx["error"] = str(exc)
    return render(request, "marketing/index.html", ctx)


# ---------------- Hub ERP (tableau de bord admin) ----------------
@staff_member_required
def orders_csv(request):
    import csv
    from django.http import HttpResponse
    from .models import Order
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="commandes_paintit.csv"'
    w = csv.writer(resp)
    w.writerow(["uid", "date", "statut", "format", "couleurs", "total_eur", "cout_eur",
                "client", "email", "ville", "pays", "fournisseur_ref"])
    for o in Order.objects.order_by("-created_at"):
        w.writerow([o.uid, o.created_at.strftime("%Y-%m-%d %H:%M"), o.get_status_display(),
                    getattr(o, "format_label", ""), getattr(o, "colors", ""),
                    getattr(o, "total", ""), getattr(o, "cost", ""),
                    getattr(o, "customer_name", ""), getattr(o, "customer_email", ""),
                    getattr(o, "city", ""), getattr(o, "country", ""),
                    getattr(o, "supplier_ref", "")])
    return resp


@staff_member_required
def admin_hub(request):
    from django.db.models import Sum, Count
    from django.utils import timezone
    from .models import Order, DigitalCanvas, ContactMessage, KitFormat
    import datetime
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    orders = Order.objects.all()
    agg = orders.aggregate(ca=Sum("total"), cost=Sum("cost"), n=Count("id"))
    ca = agg["ca"] or 0.0
    cost = agg["cost"] or 0.0
    month = orders.filter(created_at__gte=month_start).aggregate(ca=Sum("total"), n=Count("id"))
    by_status = {s: c for s, c in orders.values_list("status").annotate(c=Count("id"))}
    kpis = {
        "orders": agg["n"] or 0,
        "revenue": round(ca, 2),
        "margin": round(ca - cost, 2),
        "margin_pct": round((ca - cost) / ca * 100, 1) if ca else 0,
        "month_revenue": round(month["ca"] or 0.0, 2),
        "month_orders": month["n"] or 0,
        "canvases": DigitalCanvas.objects.exclude(email="__library__").count(),
        "gallery": DigitalCanvas.objects.filter(email="__library__").count(),
        "messages": ContactMessage.objects.count(),
        "formats": KitFormat.objects.filter(available=True).count(),
    }
    # Graphique CA sur 30 jours
    from django.db.models.functions import TruncDate
    d30 = now - datetime.timedelta(days=29)
    by_day = {r["d"]: float(r["ca"] or 0) for r in
              orders.filter(created_at__gte=d30).annotate(d=TruncDate("created_at"))
              .values("d").annotate(ca=Sum("total"))}
    series = []
    for i in range(30):
        day = (d30 + datetime.timedelta(days=i)).date()
        series.append({"day": day.strftime("%d/%m"), "ca": round(by_day.get(day, 0.0), 2)})
    max_ca = max([x["ca"] for x in series] + [1.0])
    for x in series:
        x["h"] = round(x["ca"] / max_ca * 100, 1)
    # Alertes "a traiter"
    from .models import Supplier
    pending = orders.filter(status=Order.PENDING).count()
    failed = orders.filter(status=Order.FAILED).count()
    to_ship = orders.filter(status=Order.PAID).count()
    alerts = []
    if pending:
        alerts.append({"label": "Commandes en attente de paiement", "count": pending,
                       "url": "/admin/studio/order/?status__exact=" + Order.PENDING, "level": "warn"})
    if failed:
        alerts.append({"label": "Commandes en echec", "count": failed,
                       "url": "/admin/studio/order/?status__exact=" + Order.FAILED, "level": "err"})
    if to_ship:
        alerts.append({"label": "Payees a envoyer au fournisseur", "count": to_ship,
                       "url": "/admin/studio/order/?status__exact=" + Order.PAID, "level": "info"})
    try:
        if not Supplier.for_checkout("kit"):
            alerts.append({"label": "Aucun fournisseur pour le checkout Kit", "count": "!",
                           "url": "/admin/studio/supplier/add/", "level": "err"})
    except Exception:
        pass
    # Repartition par statut (avec libelles)
    labels = dict(Order.STATUS_CHOICES)
    status_rows = [{"label": labels.get(st, st), "count": c,
                    "url": "/admin/studio/order/?status__exact=" + st}
                   for st, c in sorted(by_status.items(), key=lambda kv: -kv[1])]
    recent_orders = list(orders.order_by("-created_at")[:8])
    recent_msgs = list(ContactMessage.objects.order_by("-created_at")[:6])
    tools = [
        {"name": "Grille tarifaire", "desc": "Formats kit & tableau, prix, options", "url": "/admin-tarifs/", "icon": "\U0001F4B6"},
        {"name": "Marketing Corner", "desc": "Promos + GIF pub a partir d'une image", "url": "/marketing/", "icon": "\U0001F4E3"},
        {"name": "PBN Lab", "desc": "Pipeline pas a pas (R&D)", "url": "/pbn/", "icon": "\U0001F9EA"},
        {"name": "Suivi financier", "desc": "CA, marge, graphiques", "url": "/dashboard/", "icon": "\U0001F4C8"},
        {"name": "Commandes", "desc": "Suivi, fichiers fournisseur (.tiff)", "url": "/admin/studio/order/", "icon": "\U0001F4E6"},
        {"name": "Galerie / modeles", "desc": "Toiles, prix, apercus", "url": "/admin/studio/digitalcanvas/", "icon": "\U0001F5BC"},
        {"name": "Messages contact", "desc": "Demandes + pieces jointes", "url": "/admin/studio/contactmessage/", "icon": "\U0001F4E8"},
        {"name": "Fournisseurs", "desc": "Connexion checkout, tarifs, API/mail", "url": "/admin/studio/supplier/", "icon": "\U0001F3ED"},
        {"name": "Remises / parrainage", "desc": "Codes et taux", "url": "/admin/studio/discount/", "icon": "\U0001F3AB"},
        {"name": "Admin Django", "desc": "Tous les modeles", "url": "/admin/", "icon": "\u2699\uFE0F"},
    ]
    return render(request, "admin/hub.html", {
        "kpis": kpis, "recent_orders": recent_orders, "recent_msgs": recent_msgs,
        "tools": tools, "by_status": by_status, "series": series, "max_ca": round(max_ca, 2),
        "alerts": alerts, "status_rows": status_rows})


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
    _grant_gallery(request, m, verified)               # demo : credit immediat
    return redirect("studio:digipaint", uid=uid)


def gallery_buy_success(request):
    uid = request.GET.get("uid", ""); sid = request.GET.get("sid", "")
    verified = request.session.get("verified_email")
    if uid and sid and verified and payments.stripe_live():
        paid, _mu = payments.session_is_paid(sid)
        if paid:
            m = DigitalCanvas.objects.filter(email="__library__", uid=uid).first()
            if m:
                _grant_gallery(request, m, verified)
    return redirect("studio:digipaint", uid=uid)
