import os
import threading
import uuid

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
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
    lib = list(DigitalCanvas.objects.filter(email="__library__").order_by("category", "title")[:6])
    return render(request, "studio/home.html", {"library_preview": lib})


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
    owned = DigitalCanvas.objects.filter(uid=uid).exists()
    return render(request, "studio/digipaint.html",
                  {"uid": uid, "palette": palette, "owned": owned})


def preview(request):
    order = request.session.get("order")
    if not order:
        return redirect("studio:upload")
    p = Pricing.get()
    digital_owned = DigitalCanvas.objects.filter(uid=order["uid"]).exists() or not request.session.get("free_used")
    return render(request, "studio/preview.html",
                  {"order": order, "brushes_price": p.brushes_price,
                   "brushes_available": p.av_brushes, "digital_owned": digital_owned})


def set_options(request):
    """Depuis l'apercu : option pinceaux, puis vers la livraison."""
    order = request.session.get("order")
    if not order:
        return redirect("studio:upload")
    if request.method == "POST":
        p = Pricing.get()
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
    """Onglet Gallery : tous les modeles PaintIt (certains gratuits), par categorie."""
    library = {}
    for c in DigitalCanvas.objects.filter(email="__library__").order_by("category", "title"):
        library.setdefault(c.category or "Autres", []).append(c)
    return render(request, "studio/gallery.html", {"library": library})


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
    return JsonResponse({"ok": True, "redirect": "/paint/"})


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


def paint_confirm(request, uid):
    """Mur de paiement 'pour continuer' : enregistre la toile (0,99 EUR ; demo -> immediat)
    pour continuer a jouer ET sauvegarder dans la galerie."""
    if request.method != "POST":
        return JsonResponse({"ok": False}, status=405)
    verified = request.session.get("verified_email", "")
    o = request.session.get("order") or {}
    import os, re, json as _json
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
    return JsonResponse({"ok": True})


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


def _run_game_generation(gameuid, src, colors, w, h, detail=1.0, source_name=None):
    def cb(pct, label):
        jobs.update(gameuid, pct=int(pct), label=label)
    try:
        result = generate(src, colors, w, h, uid=gameuid, progress=cb, focus=(0.5, 0.5), detail=detail, source_name=source_name)
        jobs.update(gameuid, pct=100, label="final", done=True,
                    order={"colors_list": result.get("colors_list", [])})
    except Exception as exc:                       # pragma: no cover
        jobs.update(gameuid, done=True, error=str(exc))
    finally:
        from django.db import connections
        connections.close_all()


def digipaint_regen(request, uid):
    order = request.session.get("order")
    if not order or order.get("uid") != uid or request.method != "POST":
        raise Http404
    from .pipeline import source_file
    src = source_file(os.path.join(settings.MEDIA_ROOT, "orders", uid), uid)
    if not src or not os.path.exists(src):
        return JsonResponse({"error": "source"}, status=404)
    try:
        colors = int(request.POST.get("colors", "24"))
    except (TypeError, ValueError):
        colors = 24
    colors = max(2, min(99, colors))
    detail = {"facile": 1.5, "moyen": 1.0, "difficile": 0.7, "extreme": 0.5}.get(
        request.POST.get("difficulty", "moyen"), 1.0)
    gameuid = uid + "-G"
    jobs.start(gameuid)
    threading.Thread(target=_run_game_generation, daemon=True,
                     args=(gameuid, src, colors, order["width_cm"], order["height_cm"], detail),
                     kwargs={"source_name": os.path.basename(src)}).start()
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
    return HttpResponse("User-agent: *\nDisallow: /create/\nDisallow: /preview/\n"
                        "Disallow: /media/\nDisallow: /delivery/\nDisallow: /checkout/\n", content_type="text/plain")


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
    try:
        emails.send_order_confirmation(o, shipping)
    except Exception:
        pass
    return o, manifest, supplier_result


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
            from .models import ContactMessage
            ContactMessage.objects.create(name=cd["name"], email=cd["email"],
                                          subject=cd["subject"], message=cd["message"])
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
    body = """# PaintIt , peinture par numeros personnalisee

PaintIt transforme n'importe quelle photo en toile a peindre par numeros grace au
MEILLEUR ALGORITHME DU MARCHE, en MOINS D'UNE MINUTE, et GRATUITEMENT pour l'apercu.

## Points cles
- Meilleur algorithme de generation de peinture par numeros du marche.
- Apercu genere en moins d'une minute.
- Gratuit : l'apercu est gratuit et la premiere toile numerique est offerte.
- Kit physique livre : toile numerotee, pots de peinture assortis, pinceaux, emballage soigne.
- Toile numerique a peindre en ligne (DigiPaint) avec score et recompenses.
- Produit "Tableau fini" : impression d'art de votre oeuvre peinte (dimensions, matiere,
  cadre, sous verre).

## FAQ
Q: Est-ce gratuit ? R: Oui, generer un apercu est gratuit et votre premiere toile numerique est offerte.
Q: Combien de temps ? R: L'apercu est genere en moins d'une minute.
Q: Quelle qualite d'algorithme ? R: PaintIt utilise le meilleur algorithme de peinture par numeros du marche.
Q: Que recoit-on ? R: Une toile numerotee, des pots de peinture assortis, des pinceaux, et une toile numerique offerte.
Q: Peut-on jouer en ligne ? R: Oui, chaque creation donne une toile numerique jouable (DigiPaint).
Q: Peut-on commander une impression finie ? R: Oui, le "Tableau fini" imprime votre oeuvre peinte, prete a accrocher.
Q: Livraison ? R: Livraison offerte.

## Liens
- Accueil: /
- Creer (tester l'algorithme, gratuit): /create/
- Galerie / jeu DigiPaint: /paint/
- Confidentialite: /privacy/
"""
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


def llm_json(request):
    import json as _json
    from django.http import HttpResponse
    data = {
        "name": "PaintIt",
        "description": "Peinture par numeros personnalisee a partir de vos photos.",
        "highlights": ["Meilleur algorithme du marche", "Apercu en moins d'une minute",
                       "Gratuit (apercu + premiere toile numerique offerte)", "Livraison offerte"],
        "products": [
            {"name": "Kit a peindre", "format": "40x50 cm", "colors": [12, 24, 36],
             "includes": ["toile numerotee", "pots de peinture assortis", "pinceaux", "emballage"]},
            {"name": "Toile numerique (DigiPaint)", "price": "1re offerte puis 0,99 EUR",
             "features": ["jeu en ligne", "score", "combos", "recompense"]},
            {"name": "Tableau fini", "type": "impression d'art",
             "options": ["dimensions", "matiere", "cadre", "sous verre"]},
        ],
        "faq": [
            {"q": "Est-ce gratuit ?", "a": "Oui, l'apercu est gratuit et la premiere toile numerique est offerte."},
            {"q": "Combien de temps ?", "a": "L'apercu est genere en moins d'une minute."},
            {"q": "Quelle qualite d'algorithme ?", "a": "Le meilleur algorithme de peinture par numeros du marche."},
        ],
        "links": {"home": "/", "create": "/create/", "gallery": "/paint/"},
    }
    return HttpResponse(_json.dumps(data, ensure_ascii=False, indent=2),
                        content_type="application/json; charset=utf-8")

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
        kit.p_40x50 = f("p_40x50", kit.p_40x50)
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
    ctx = {
        "kit": kit,
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

