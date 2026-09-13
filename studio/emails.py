"""E-mails transactionnels PaintIt (brandes, localises selon le pays de livraison)."""

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives

import logging
logger = logging.getLogger("studio.emails")


def _safe_send(msg):
    """Envoi robuste (prod) : ne casse jamais la requete, mais LOGGE les echecs."""
    try:
        sent = msg.send(fail_silently=False)
        if not sent:
            logger.warning("Email non envoye (0 destinataire accepte) subject=%r to=%r",
                           getattr(msg, "subject", ""), getattr(msg, "to", ""))
        return bool(sent)
    except Exception:
        logger.exception("Echec envoi email subject=%r to=%r",
                         getattr(msg, "subject", ""), getattr(msg, "to", ""))
        return False


from .fulfillment import lang_for_country

STR = {
    "fr": {"subject": "Votre commande PaintIt %(uid)s est confirmée",
           "hi": "Bonjour", "confirmed": "Votre commande %(uid)s est confirmée.",
           "thanks": "Merci pour votre commande !",
           "product": "Produit", "colors": "couleurs", "brushes": "Set de pinceaux",
           "subtotal": "Sous-total", "discount": "Remise", "shipping": "Livraison",
           "free": "Offerte", "total": "Total", "deliver_to": "Livraison",
           "phone": "Tél.", "prep": "Nous préparons votre toile et vos pots de peinture "
           "numérotés. Vous recevrez un suivi dès l'expédition.",
           "team": "L'équipe PaintIt", "orient": {"portrait": "portrait", "paysage": "paysage"}},
    "en": {"subject": "Your PaintIt order %(uid)s is confirmed",
           "hi": "Hello", "confirmed": "Your order %(uid)s is confirmed.",
           "thanks": "Thank you for your order!",
           "product": "Product", "colors": "colors", "brushes": "Brush set",
           "subtotal": "Subtotal", "discount": "Discount", "shipping": "Shipping",
           "free": "Free", "total": "Total", "deliver_to": "Delivery",
           "phone": "Phone", "prep": "We're preparing your canvas and numbered paint pots. "
           "You'll get tracking as soon as it ships.",
           "team": "The PaintIt team", "orient": {"portrait": "portrait", "paysage": "landscape"}},
    "de": {"subject": "Ihre PaintIt-Bestellung %(uid)s ist bestätigt",
           "hi": "Hallo", "confirmed": "Ihre Bestellung %(uid)s ist bestätigt.",
           "thanks": "Danke für Ihre Bestellung!",
           "product": "Produkt", "colors": "Farben", "brushes": "Pinselset",
           "subtotal": "Zwischensumme", "discount": "Rabatt", "shipping": "Versand",
           "free": "Kostenlos", "total": "Gesamt", "deliver_to": "Lieferung",
           "phone": "Tel.", "prep": "Wir bereiten Ihre Leinwand und die nummerierten "
           "Farbtöpfe vor. Sie erhalten die Sendungsverfolgung beim Versand.",
           "team": "Ihr PaintIt-Team", "orient": {"portrait": "Hochformat", "paysage": "Querformat"}},
    "es": {"subject": "Tu pedido PaintIt %(uid)s está confirmado",
           "hi": "Hola", "confirmed": "Tu pedido %(uid)s está confirmado.",
           "thanks": "¡Gracias por tu pedido!",
           "product": "Producto", "colors": "colores", "brushes": "Set de pinceles",
           "subtotal": "Subtotal", "discount": "Descuento", "shipping": "Envío",
           "free": "Gratis", "total": "Total", "deliver_to": "Entrega",
           "phone": "Tel.", "prep": "Estamos preparando tu lienzo y los botes de pintura "
           "numerados. Recibirás el seguimiento en cuanto se envíe.",
           "team": "El equipo PaintIt", "orient": {"portrait": "vertical", "paysage": "horizontal"}},
}




def send_order_confirmation(order, shipping):
    to = (shipping or {}).get("email")
    if not to:
        return
    T = STR.get((order.get("lang") or "en")[:2], STR["en"])
    uid = order["uid"]
    d = order.get("discount")
    total = order.get("total", order["price"])
    brushes_amt = order.get("brushes_amount", 0.0)
    canvas = round(order["price"] - brushes_amt, 2)
    orient = T["orient"].get(order.get("orientation", ""), order.get("orientation", ""))
    addr2 = (", " + shipping["address2"]) if shipping.get("address2") else ""
    phone = ("%s %s" % (shipping.get("phone_code", ""), shipping.get("phone", ""))).strip()

    # Texte brut (repli)
    lines = [
        "%s %s," % (T["hi"], shipping.get("full_name", "")), "",
        T["confirmed"] % {"uid": uid}, "",
        "%s : %s (%s), %s %s" % (T["product"], order["format_label"], orient,
                                 order["colors"], T["colors"]),
        "%s : %s EUR" % (T["subtotal"], canvas),
    ]
    if brushes_amt:
        lines.append("%s : +%s EUR" % (T["brushes"], brushes_amt))
    if d:
        lines.append("%s (%s) : -%s EUR" % (T["discount"], d["code"], d["amount"]))
    lines += ["%s : %s" % (T["shipping"], T["free"]),
              "%s : %s EUR" % (T["total"], total), "",
              "%s :" % T["deliver_to"], shipping.get("full_name", ""),
              shipping.get("address1", "") + addr2,
              "%s %s (%s)" % (shipping.get("postal_code", ""), shipping.get("city", ""),
                              shipping.get("country", "")),
              "%s : %s" % (T["phone"], phone), "", T["prep"], "", T["team"]]
    text = "\n".join(lines)

    d_row = ('<tr><td style="padding:6px 0;color:#5b647a">%s (%s)</td>'
             '<td align="right" style="padding:6px 0">-%s &euro;</td></tr>'
             % (T["discount"], d["code"], d["amount"])) if d else ""
    b_row = ('<tr><td style="padding:6px 0;color:#5b647a">%s</td>'
             '<td align="right" style="padding:6px 0">+%s &euro;</td></tr>'
             % (T["brushes"], brushes_amt)) if brushes_amt else ""

    html = (
        '<div style="background:#eef2f9;padding:24px 0;font-family:Arial,Helvetica,sans-serif">'
        '<table align="center" width="560" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background:#fff;border-radius:16px;overflow:hidden;'
        'box-shadow:0 6px 24px rgba(18,34,79,.08)">'
        '<tr><td style="background:#12224f;padding:18px 24px">'
        '<img src="%(logo)s" alt="PaintIt" height="26" style="height:26px;background:#fff;'
        'border-radius:8px;padding:5px 8px"></td></tr>'
        '<tr><td style="padding:26px 28px 8px">'
        '<h1 style="margin:0 0 6px;color:#12224f;font-size:20px">%(thanks)s</h1>'
        '<p style="margin:0 0 2px;color:#12224f">%(hi)s %(name)s,</p>'
        '<p style="margin:0;color:#5b647a">%(confirmed)s</p></td></tr>'
        '<tr><td style="padding:6px 28px 4px">'
        '<table width="100%%" cellpadding="0" cellspacing="0" style="font-size:14px;color:#12224f">'
        '<tr><td style="padding:6px 0;color:#5b647a">%(product)s</td>'
        '<td align="right" style="padding:6px 0">%(fmt)s (%(orient)s), %(colors)s %(clabel)s</td></tr>'
        '<tr><td style="padding:6px 0;color:#5b647a">%(subtotal)s</td>'
        '<td align="right" style="padding:6px 0">%(canvas)s &euro;</td></tr>'
        '%(brow)s%(drow)s'
        '<tr><td style="padding:6px 0;color:#5b647a">%(shipping)s</td>'
        '<td align="right" style="padding:6px 0;color:#237a3e;font-weight:bold">%(free)s</td></tr>'
        '<tr><td style="padding:10px 0 0;border-top:1px solid #e2e8f2;font-weight:bold">%(total_l)s</td>'
        '<td align="right" style="padding:10px 0 0;border-top:1px solid #e2e8f2;font-weight:bold">'
        '%(total)s &euro;</td></tr></table></td></tr>'
        '<tr><td style="padding:8px 28px 4px;color:#5b647a;font-size:13px;line-height:1.5">'
        '<b style="color:#12224f">%(deliver_to)s</b><br>%(name)s<br>%(addr1)s%(addr2)s<br>'
        '%(postal)s %(city)s (%(country)s)<br>%(phone_l)s : %(phone)s</td></tr>'
        '<tr><td style="padding:14px 28px 4px;color:#12224f;font-size:14px">%(prep)s</td></tr>'
        '<tr><td style="padding:6px 28px 24px"><b style="color:#2f6bf2">%(team)s</b></td></tr>'
        '</table></div>'
    ) % {
        "logo": settings.SITE_URL + settings.STATIC_URL + "studio/img/logo.png",
        "thanks": T["thanks"], "hi": T["hi"], "name": shipping.get("full_name", ""),
        "confirmed": T["confirmed"] % {"uid": uid},
        "product": T["product"], "fmt": order["format_label"], "orient": orient,
        "colors": order["colors"], "clabel": T["colors"],
        "subtotal": T["subtotal"], "canvas": canvas, "brow": b_row, "drow": d_row,
        "shipping": T["shipping"], "free": T["free"], "total_l": T["total"], "total": total,
        "deliver_to": T["deliver_to"], "addr1": shipping.get("address1", ""), "addr2": addr2,
        "postal": shipping.get("postal_code", ""), "city": shipping.get("city", ""),
        "country": shipping.get("country", ""), "phone_l": T["phone"], "phone": phone,
        "prep": T["prep"], "team": T["team"],
    }

    subject = T["subject"] % {"uid": uid}
    msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [to])
    msg.attach_alternative(html, "text/html")
    _safe_send(msg)


def send_contact(name, email, subject, message, attachments=None):
    body = "De : %s <%s>\nSujet : %s\n\n%s" % (name, email, subject, message)
    msg = EmailMessage(subject="[Contact PaintIt] %s" % subject, body=body,
                       from_email=settings.DEFAULT_FROM_EMAIL, to=[settings.SUPPORT_EMAIL],
                       reply_to=[email])
    for fn, content, mimetype in (attachments or []):
        msg.attach(fn, content, mimetype)
    _safe_send(msg)


FEEDBACK_STR = {
    "fr": {"subject": "Votre avis sur votre commande PaintIt ?",
           "hi": "Bonjour", "body": "Nous esperons que votre tableau vous plait ! Votre avis nous aide enormement. Prendriez-vous une minute pour le partager ?",
           "cta": "Laisser un avis", "reply": "Ou repondez simplement a cet e-mail.",
           "thanks": "Merci et bonne peinture !", "team": "L'equipe PaintIt"},
    "en": {"subject": "How was your PaintIt order?",
           "hi": "Hello", "body": "We hope you love your canvas! Your feedback means a lot. Would you take a minute to share it?",
           "cta": "Leave a review", "reply": "Or simply reply to this email.",
           "thanks": "Thanks and happy painting!", "team": "The PaintIt team"},
    "de": {"subject": "Wie war Ihre PaintIt-Bestellung?",
           "hi": "Hallo", "body": "Wir hoffen, Ihre Leinwand gefaellt Ihnen! Ihr Feedback bedeutet uns viel. Nehmen Sie sich eine Minute Zeit?",
           "cta": "Bewertung abgeben", "reply": "Oder antworten Sie einfach auf diese E-Mail.",
           "thanks": "Danke und viel Freude beim Malen!", "team": "Ihr PaintIt-Team"},
    "es": {"subject": "\u00bfQue te parecio tu pedido PaintIt?",
           "hi": "Hola", "body": "\u00a1Esperamos que te encante tu lienzo! Tu opinion nos ayuda mucho. \u00bfNos dedicas un minuto para compartirla?",
           "cta": "Dejar una opinion", "reply": "O simplemente responde a este correo.",
           "thanks": "\u00a1Gracias y feliz pintura!", "team": "El equipo PaintIt"},
}


def send_feedback_request(order_row):
    o = order_row
    if not o.customer_email:
        return
    T = FEEDBACK_STR.get((o.lang or "en")[:2], FEEDBACK_STR["en"])
    logo = settings.SITE_URL + settings.STATIC_URL + "studio/img/logo.png"
    review = getattr(settings, "REVIEW_URL", "")
    cta = ('<p style="margin:18px 0"><a href="%s" style="background:#2f6bf2;color:#fff;'
           'text-decoration:none;font-weight:700;padding:12px 20px;border-radius:10px;'
           'display:inline-block">%s</a></p>' % (review, T["cta"])) if review else ""
    reply_line = "" if review else T["reply"]
    text = "%s %s,\n\n%s\n%s\n\n%s\n%s" % (
        T["hi"], o.customer_name, T["body"], (review or T["reply"]), T["thanks"], T["team"])
    html = (
        '<div style="background:#eef2f9;padding:24px 0;font-family:Arial,Helvetica,sans-serif">'
        '<table align="center" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;'
        'background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 6px 24px rgba(18,34,79,.08)">'
        '<tr><td style="background:#12224f;padding:18px 24px"><img src="%s" alt="PaintIt" '
        'height="26" style="height:26px;background:#fff;border-radius:8px;padding:5px 8px"></td></tr>'
        '<tr><td style="padding:26px 28px;color:#12224f;font-size:15px;line-height:1.6">'
        '<p style="margin:0 0 6px"><b>%s %s,</b></p>'
        '<p style="margin:0 0 10px;color:#5b647a">%s</p>%s'
        '<p style="margin:0;color:#5b647a">%s</p>'
        '<p style="margin:16px 0 0">%s</p>'
        '<p style="margin:4px 0 0"><b style="color:#2f6bf2">%s</b></p>'
        '</td></tr></table></div>') % (
        logo, T["hi"], o.customer_name, T["body"], cta, reply_line, T["thanks"], T["team"])
    msg = EmailMultiAlternatives(T["subject"], text, settings.DEFAULT_FROM_EMAIL, [o.customer_email])
    msg.attach_alternative(html, "text/html")
    _safe_send(msg)


GALLERY_STR = {
    "fr": {"subject": "Vos creations PaintIt", "hi": "Bonjour",
           "intro": "Voici vos toiles enregistrees. Rejouez en DigiPaint ou commandez la version reelle.",
           "code": "Code", "play": "Jouer", "note": "Astuce : dans le jeu, saisissez simplement votre code pour retrouver un design.",
           "team": "L'equipe PaintIt"},
    "en": {"subject": "Your PaintIt creations", "hi": "Hello",
           "intro": "Here are your saved canvases. Replay in DigiPaint or order the real version.",
           "code": "Code", "play": "Play", "note": "Tip: in the game, just enter your code to reopen a design.",
           "team": "The PaintIt team"},
    "de": {"subject": "Ihre PaintIt-Werke", "hi": "Hallo",
           "intro": "Hier sind Ihre gespeicherten Leinwaende. Spielen Sie in DigiPaint oder bestellen Sie die echte Version.",
           "code": "Code", "play": "Spielen", "note": "Tipp: Geben Sie im Spiel einfach Ihren Code ein, um ein Design wieder zu oeffnen.",
           "team": "Ihr PaintIt-Team"},
    "es": {"subject": "Tus creaciones PaintIt", "hi": "Hola",
           "intro": "Aqui tienes tus lienzos guardados. Vuelve a jugar en DigiPaint o pide la version real.",
           "code": "Codigo", "play": "Jugar", "note": "Consejo: en el juego, introduce tu codigo para reabrir un diseno.",
           "team": "El equipo PaintIt"},
}


def send_gallery(email, models, lang="fr"):
    import os
    from django.conf import settings as st
    T = GALLERY_STR.get((lang or "en")[:2], GALLERY_STR["en"])
    site = st.SITE_URL
    logo = site + st.STATIC_URL + "studio/img/logo.png"
    rows_html, rows_txt = [], []
    for m in models:
        uid = str(m.get("uid", "")).strip()
        if not uid or not os.path.isdir(os.path.join(st.MEDIA_ROOT, "orders", uid)):
            continue
        play = site + "/paint/" + uid + "/"
        thumb = site + "/preview/img/" + uid + "/preview/"
        meta = "%s couleurs" % m.get("colors", "")
        rows_html.append(
            '<tr><td style="padding:8px 0;width:74px"><img src="%s" width="64" '
            'style="width:64px;border-radius:8px;border:1px solid #e2e8f2"></td>'
            '<td style="padding:8px 10px;color:#12224f;font-size:14px">'
            '<b>%s</b> &middot; <span style="color:#5b647a">%s</span><br>'
            '<span style="color:#5b647a">%s : </span><code>%s</code> &nbsp; '
            '<a href="%s" style="color:#2f6bf2;font-weight:700">%s &rarr;</a></td></tr>'
            % (thumb, uid, meta, T["code"], uid, play, T["play"]))
        rows_txt.append("%s (%s) - %s : %s\n%s" % (uid, meta, T["code"], uid, play))
    if not rows_html:
        return
    text = "%s,\n\n%s\n\n%s\n\n%s\n%s" % (T["hi"], T["intro"], "\n\n".join(rows_txt), T["note"], T["team"])
    html = (
        '<div style="background:#eef2f9;padding:24px 0;font-family:Arial,Helvetica,sans-serif">'
        '<table align="center" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;'
        'background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 6px 24px rgba(18,34,79,.08)">'
        '<tr><td style="background:#12224f;padding:18px 24px"><img src="%s" alt="PaintIt" height="26" '
        'style="height:26px;background:#fff;border-radius:8px;padding:5px 8px"></td></tr>'
        '<tr><td style="padding:22px 28px 6px;color:#12224f"><p style="margin:0 0 4px"><b>%s,</b></p>'
        '<p style="margin:0;color:#5b647a">%s</p></td></tr>'
        '<tr><td style="padding:6px 28px"><table width="100%%" cellpadding="0" cellspacing="0">%s</table></td></tr>'
        '<tr><td style="padding:10px 28px 22px;color:#5b647a;font-size:13px">%s<br><br>'
        '<b style="color:#2f6bf2">%s</b></td></tr></table></div>'
    ) % (logo, T["hi"], T["intro"], "".join(rows_html), T["note"], T["team"])
    msg = EmailMultiAlternatives(T["subject"], text, st.DEFAULT_FROM_EMAIL, [email])
    msg.attach_alternative(html, "text/html")
    _safe_send(msg)


def send_code(email, code, lang="fr"):
    subj = {"fr": "Votre code PaintIt", "en": "Your PaintIt code",
            "de": "Ihr PaintIt-Code", "es": "Tu codigo PaintIt"}.get((lang or "en")[:2], "Your PaintIt code")
    body = {"fr": "Votre code de verification : %s\n\nIl est valable 15 minutes." % code,
            "en": "Your verification code: %s\n\nValid for 15 minutes." % code,
            "de": "Ihr Bestaetigungscode: %s\n\n15 Minuten gueltig." % code,
            "es": "Tu codigo de verificacion: %s\n\nValido 15 minutos." % code}.get((lang or "en")[:2],
            "Your verification code: %s" % code)
    from django.core.mail import EmailMessage
    from django.conf import settings as st
    _safe_send(EmailMessage(subj, body, st.DEFAULT_FROM_EMAIL, [email]))
