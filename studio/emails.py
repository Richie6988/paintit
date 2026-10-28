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

def _brand(html):
    """Ajoute un pied de page avec lien vers le site + rend le logo cliquable."""
    site = settings.SITE_URL
    logo = site + settings.STATIC_URL + "studio/img/logo.png"
    # logo -> lien vers le site
    html = html.replace('<img src="%s"' % logo,
                        '</a>' and ('<a href="%s" target="_blank" style="text-decoration:none"><img src="%s"' % (site, logo)))
    # (referme le <a> juste apres la balise img du header)
    html = html.replace('border-radius:8px;padding:5px 8px"></td>',
                        'border-radius:8px;padding:5px 8px"></a></td>', 1)
    # pied de page
    footer = ('<tr><td style="padding:16px 24px;background:#f4f6fb;text-align:center;'
              'color:#5b647a;font-size:13px;font-family:Arial,Helvetica,sans-serif">'
              'PaintIt , <a href="%s" target="_blank" style="color:#2f6bf2;font-weight:700;'
              'text-decoration:none">%s</a></td></tr>' % (site, site.replace("https://", "").replace("http://", "")))
    html = html.replace('</table>', footer + '</table>', 1)
    return html




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
    "it": {"subject": "Il tuo ordine PaintIt %(uid)s è confermato",
           "hi": "Ciao", "confirmed": "Il tuo ordine %(uid)s è confermato.",
           "thanks": "Grazie per il tuo ordine!",
           "product": "Prodotto", "colors": "colori", "brushes": "Set di pennelli",
           "subtotal": "Subtotale", "discount": "Sconto", "shipping": "Spedizione",
           "free": "Gratuita", "total": "Totale", "deliver_to": "Consegna",
           "phone": "Tel.", "prep": "Stiamo preparando la tua tela e i vasetti di colore "
           "numerati. Riceverai il tracciamento appena verrà spedito.",
           "team": "Il team PaintIt", "orient": {"portrait": "verticale", "paysage": "orizzontale"}},
}




def send_order_confirmation(order, shipping):
    to = (shipping or {}).get("email")
    if not to:
        return
    T = STR.get((order.get("lang") or "en")[:2], STR["en"])
    uid = order["uid"]
    d = order.get("discount")
    lang = (order.get("lang") or "en")[:2]
    from .currency import fmt as _fmt
    am = order.get("amounts") or {}
    cur = am.get("cur") or order.get("currency") or "EUR"
    money = lambda x: _fmt(x, cur, lang)          # montants dans la devise payee par le client
    brushes_amt = am.get("brushes", order.get("brushes_amount", 0.0)) if order.get("brushes") else 0.0
    canvas = money(am.get("canvas", order["price"] - order.get("brushes_amount", 0.0)))
    total = money(am.get("total", order.get("total", order["price"])))
    disc_amt = am.get("discount", (order.get("discount") or {}).get("amount", 0))
    orient = T["orient"].get(order.get("orientation", ""), order.get("orientation", ""))
    from .models import finish_of
    orient += ", " + {
        "ck": {"fr": "sans cadre", "en": "unframed", "de": "ohne Rahmen", "es": "sin marco", "it": "senza telaio"},
        "fk": {"fr": "cadre à monter", "en": "DIY frame", "de": "Rahmen zum Selbstaufbau", "es": "marco para montar",
               "it": "telaio da montare"},
        "nk": {"fr": "encadrée", "en": "framed", "de": "gerahmt", "es": "enmarcado", "it": "incorniciata"},
    }[finish_of(order)].get(lang, "")
    addr2 = (", " + shipping["address2"]) if shipping.get("address2") else ""
    phone = ("%s %s" % (shipping.get("phone_code", ""), shipping.get("phone", ""))).strip()

    # Texte brut (repli)
    lines = [
        "%s %s," % (T["hi"], shipping.get("full_name", "")), "",
        T["confirmed"] % {"uid": uid}, "",
        "%s : %s (%s), %s %s" % (T["product"], order["format_label"], orient,
                                 order["colors"], T["colors"]),
        "%s : %s" % (T["subtotal"], canvas),
    ]
    if brushes_amt:
        lines.append("%s : +%s" % (T["brushes"], money(brushes_amt)))
    if d:
        lines.append("%s (%s) : -%s" % (T["discount"], d["code"], money(disc_amt)))
    lines += ["%s : %s" % (T["shipping"], T["free"]),
              "%s : %s" % (T["total"], total), "",
              "%s :" % T["deliver_to"], shipping.get("full_name", ""),
              shipping.get("address1", "") + addr2,
              "%s %s (%s)" % (shipping.get("postal_code", ""), shipping.get("city", ""),
                              shipping.get("country", "")),
              "%s : %s" % (T["phone"], phone), "", T["prep"], "", T["team"]]
    text = "\n".join(lines)

    d_row = ('<tr><td style="padding:6px 0;color:#5b647a">%s (%s)</td>'
             '<td align="right" style="padding:6px 0">-%s</td></tr>'
             % (T["discount"], d["code"], money(disc_amt))) if d else ""
    b_row = ('<tr><td style="padding:6px 0;color:#5b647a">%s</td>'
             '<td align="right" style="padding:6px 0">+%s</td></tr>'
             % (T["brushes"], money(brushes_amt))) if brushes_amt else ""

    html = (
        '<div style="background:#eef2f9;padding:24px 0;font-family:Arial,Helvetica,sans-serif">'
        '<table align="center" width="560" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;background:#fff;border-radius:16px;overflow:hidden;'
        'box-shadow:0 6px 24px rgba(18,34,79,.08)">'
        '<tr><td style="background:#12224f;padding:18px 24px">'
        '<img src="%(logo)s" alt="PaintIt" height="26" style="height:26px;background:#fff;'
        'border-radius:8px;padding:5px 8px"></td></tr>'
        '<tr><td style="padding:0"><a href="%(play)s"><img src="%(hero)s" alt="" '
        'style="width:100%%;max-height:320px;object-fit:cover;display:block"></a></td></tr>'
        '<tr><td style="padding:26px 28px 8px">'
        '<h1 style="margin:0 0 6px;color:#12224f;font-size:20px">%(thanks)s</h1>'
        '<p style="margin:0 0 2px;color:#12224f">%(hi)s %(name)s,</p>'
        '<p style="margin:0;color:#5b647a">%(confirmed)s</p></td></tr>'
        '<tr><td style="padding:6px 28px 4px">'
        '<table width="100%%" cellpadding="0" cellspacing="0" style="font-size:14px;color:#12224f">'
        '<tr><td style="padding:6px 0;color:#5b647a">%(product)s</td>'
        '<td align="right" style="padding:6px 0">%(fmt)s (%(orient)s), %(colors)s %(clabel)s</td></tr>'
        '<tr><td style="padding:6px 0;color:#5b647a">%(subtotal)s</td>'
        '<td align="right" style="padding:6px 0">%(canvas)s</td></tr>'
        '%(brow)s%(drow)s'
        '<tr><td style="padding:6px 0;color:#5b647a">%(shipping)s</td>'
        '<td align="right" style="padding:6px 0;color:#237a3e;font-weight:bold">%(free)s</td></tr>'
        '<tr><td style="padding:10px 0 0;border-top:1px solid #e2e8f2;font-weight:bold">%(total_l)s</td>'
        '<td align="right" style="padding:10px 0 0;border-top:1px solid #e2e8f2;font-weight:bold">'
        '%(total)s</td></tr></table></td></tr>'
        '<tr><td style="padding:8px 28px 4px;color:#5b647a;font-size:13px;line-height:1.5">'
        '<b style="color:#12224f">%(deliver_to)s</b><br>%(name)s<br>%(addr1)s%(addr2)s<br>'
        '%(postal)s %(city)s (%(country)s)<br>%(phone_l)s : %(phone)s</td></tr>'
        '<tr><td style="padding:16px 28px 4px" align="center">'
        '<a href="%(play)s" style="display:inline-block;background:linear-gradient(90deg,#2f6bf2,#7a3ff2);'
        'color:#fff;font-weight:bold;text-decoration:none;padding:13px 26px;border-radius:999px;'
        'box-shadow:0 6px 16px rgba(47,107,242,.35);font-size:15px">%(play_l)s</a></td></tr>'
        '<tr><td style="padding:8px 28px 4px" align="center">'
        '<a href="%(gallery)s" style="color:#2f6bf2;text-decoration:none;font-weight:bold;font-size:13px">%(gallery_l)s</a></td></tr>'
        '<tr><td style="padding:14px 28px 4px;color:#12224f;font-size:14px">%(prep)s</td></tr>'
        '<tr><td style="padding:6px 28px 24px"><b style="color:#2f6bf2">%(team)s</b></td></tr>'
        '</table></div>'
    ) % {
        "logo": settings.SITE_URL + settings.STATIC_URL + "studio/img/logo.png",
        "hero": settings.SITE_URL + "/preview/img/" + uid + "/preview/",
        "play": settings.SITE_URL + "/play/" + uid + "/",
        "gallery": settings.SITE_URL + "/paint/",
        "play_l": {"fr": "\u25B6 Jouer en digital maintenant", "en": "\u25B6 Play the digital version",
                   "de": "\u25B6 Jetzt digital spielen", "es": "\u25B6 Jugar la versi\u00f3n digital",
                   "it": "\u25B6 Gioca ora la versione digitale"}.get((order.get("lang") or "en")[:2], "\u25B6 Play"),
        "gallery_l": {"fr": "Retrouver toutes mes toiles \u2192", "en": "See all my canvases \u2192",
                      "de": "Alle meine Bilder ansehen \u2192", "es": "Ver todos mis lienzos \u2192",
                      "it": "Vedi tutte le mie tele \u2192"}.get((order.get("lang") or "en")[:2], "My canvases \u2192"),
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
    msg.attach_alternative(_brand(html), "text/html")
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
    "it": {"subject": "Com'è andato il tuo ordine PaintIt?",
           "hi": "Ciao", "body": "Speriamo che la tua tela ti piaccia! La tua opinione conta molto per noi. Ci dedichi un minuto per condividerla?",
           "cta": "Lascia una recensione", "reply": "Oppure rispondi semplicemente a questa e-mail.",
           "thanks": "Grazie e buona pittura!", "team": "Il team PaintIt"},
}


def send_feedback_request(order_row):
    o = order_row
    if not o.customer_email:
        return
    T = FEEDBACK_STR.get((o.lang or "en")[:2], FEEDBACK_STR["en"])
    logo = settings.SITE_URL + settings.STATIC_URL + "studio/img/logo.png"
    from .security import review_url
    review = getattr(settings, "REVIEW_URL", "") or review_url(o.uid)   # par defaut : avis verifie sur paintit.click
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
    msg.attach_alternative(_brand(html), "text/html")
    _safe_send(msg)


SOON_STR = {
    "fr": {"subject": "Ça arrive !!! Votre toile PaintIt est en France", "hi": "Bonjour",
           "body": "Bonne nouvelle : votre kit PaintIt est arrivé et vient d'être confié à {carrier} pour la dernière ligne droite.",
           "num": "Votre numéro de suivi", "cta": "Suivre mon colis", "soon": "Préparez vos pinceaux, ça arrive très bientôt !",
           "team": "L'équipe PaintIt", "country": "France"},
    "en": {"subject": "It's coming soon!!! Your PaintIt canvas has arrived", "hi": "Hello",
           "body": "Good news: your PaintIt kit has arrived in your country and has been handed over to {carrier} for the last mile.",
           "num": "Your tracking number", "cta": "Track my parcel", "soon": "Get your brushes ready, it's coming very soon!",
           "team": "The PaintIt team"},
    "de": {"subject": "Es kommt bald!!! Ihre PaintIt-Leinwand ist angekommen", "hi": "Hallo",
           "body": "Gute Nachricht: Ihr PaintIt-Set ist in Ihrem Land angekommen und wurde {carrier} für die letzte Etappe übergeben.",
           "num": "Ihre Sendungsnummer", "cta": "Paket verfolgen", "soon": "Pinsel bereithalten, es kommt sehr bald!",
           "team": "Ihr PaintIt-Team"},
    "es": {"subject": "¡¡¡Ya llega!!! Tu lienzo PaintIt ha llegado", "hi": "Hola",
           "body": "Buenas noticias: tu kit PaintIt ha llegado a tu país y se ha entregado a {carrier} para la última etapa.",
           "num": "Tu número de seguimiento", "cta": "Seguir mi paquete", "soon": "¡Prepara los pinceles, llega muy pronto!",
           "team": "El equipo PaintIt"},
    "it": {"subject": "Sta arrivando!!! La tua tela PaintIt è arrivata", "hi": "Ciao",
           "body": "Buone notizie: il tuo kit PaintIt è arrivato nel tuo paese ed è stato affidato a {carrier} per l'ultimo tratto.",
           "num": "Il tuo numero di tracciamento", "cta": "Traccia il pacco", "soon": "Prepara i pennelli, arriva prestissimo!",
           "team": "Il team PaintIt"},
}
CARRIER_NAMES = {"la-poste-colissimo": "Colissimo", "colissimo": "Colissimo", "laposte": "La Poste", "la-poste": "La Poste",
                 "chronopost-france": "Chronopost", "dhl": "DHL", "dhl-germany": "DHL", "deutsch-post": "Deutsche Post",
                 "dpd": "DPD", "dpd-fr": "DPD", "gls": "GLS", "ups": "UPS", "fedex": "FedEx", "postnl": "PostNL",
                 "bpost": "bpost", "correos-spain": "Correos", "poste-italiane": "Poste Italiane", "colis-prive": "Colis Privé",
                 "mondialrelay": "Mondial Relay", "royal-mail": "Royal Mail", "usps": "USPS", "canada-post": "Canada Post",
                 "swiss-post": "La Poste Suisse", "austrian-post": "Österreichische Post", "an-post": "An Post"}


def send_coming_soon(order_row, last_mile):
    """Colis arrive dans le pays du client : n° du transporteur national + message « ça arrive ». -> bool"""
    o = order_row
    if not o.customer_email or not last_mile.get("number"):
        return False
    T = SOON_STR.get((o.lang or "en")[:2], SOON_STR["en"])
    slug = (last_mile.get("carrier") or "").lower()
    carrier = CARRIER_NAMES.get(slug) or (slug.replace("-", " ").title() if slug else "")
    carrier = carrier or {"fr": "notre transporteur", "de": "unserem Zusteller", "es": "nuestro transportista",
                          "it": "il nostro corriere"}.get((o.lang or "en")[:2], "our carrier")
    subject = T["subject"] if (o.lang or "")[:2] != "fr" or o.country in ("France", "FR") else \
        "Ça arrive !!! Votre toile PaintIt est arrivée"     # client francophone livre hors de France
    url = last_mile.get("url") or ""
    body = T["body"].format(carrier=carrier)
    logo = settings.SITE_URL + settings.STATIC_URL + "studio/img/logo.png"
    text = "%s %s,\n\n%s\n\n%s : %s %s\n%s\n\n%s\n%s" % (
        T["hi"], o.customer_name, body, T["num"], carrier, last_mile["number"], url, T["soon"], T["team"])
    cta = ('<p style="margin:18px 0"><a href="%s" style="background:#2f6bf2;color:#fff;text-decoration:none;'
           'font-weight:700;padding:12px 20px;border-radius:10px;display:inline-block">%s</a></p>' % (url, T["cta"])) if url else ""
    html = (
        '<div style="background:#eef2f9;padding:24px 0;font-family:Arial,Helvetica,sans-serif">'
        '<table align="center" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;'
        'background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 6px 24px rgba(18,34,79,.08)">'
        '<tr><td style="background:#12224f;padding:18px 24px"><img src="%s" alt="PaintIt" '
        'height="26" style="height:26px;background:#fff;border-radius:8px;padding:5px 8px"></td></tr>'
        '<tr><td style="padding:26px 28px;color:#12224f;font-size:15px;line-height:1.6">'
        '<p style="margin:0 0 4px;font-size:22px;font-weight:800">🎨 %s</p>'
        '<p style="margin:12px 0 6px"><b>%s %s,</b></p><p style="margin:0 0 14px;color:#5b647a">%s</p>'
        '<div style="background:#f4f6fb;border-radius:12px;padding:14px 16px"><div style="font-size:12px;color:#5b647a;'
        'text-transform:uppercase;letter-spacing:.05em;font-weight:700">%s</div>'
        '<div style="font-size:18px;font-weight:800;margin-top:4px">%s · %s</div></div>%s'
        '<p style="margin:16px 0 0;font-weight:700">%s</p><p style="margin:4px 0 0"><b style="color:#2f6bf2">%s</b></p>'
        '</td></tr></table></div>') % (
        logo, subject, T["hi"], o.customer_name, body, T["num"], carrier, last_mile["number"], cta, T["soon"], T["team"])
    msg = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [o.customer_email])
    msg.attach_alternative(_brand(html), "text/html")
    return _safe_send(msg)


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
    "it": {"subject": "Le tue creazioni PaintIt", "hi": "Ciao",
           "intro": "Ecco le tue tele salvate. Rigioca in DigiPaint o ordina la versione reale.",
           "code": "Codice", "play": "Gioca", "note": "Suggerimento: nel gioco, inserisci il tuo codice per riaprire un design.",
           "team": "Il team PaintIt"},
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
        play = site + "/play/" + uid + "/"
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
    msg.attach_alternative(_brand(html), "text/html")
    _safe_send(msg)


def send_code(email, code, lang="fr"):
    subj = {"fr": "Votre code PaintIt", "en": "Your PaintIt code",
            "de": "Ihr PaintIt-Code", "es": "Tu codigo PaintIt",
            "it": "Il tuo codice PaintIt"}.get((lang or "en")[:2], "Your PaintIt code")
    body = {"fr": "Votre code de verification : %s\n\nIl est valable 15 minutes." % code,
            "en": "Your verification code: %s\n\nValid for 15 minutes." % code,
            "de": "Ihr Bestaetigungscode: %s\n\n15 Minuten gueltig." % code,
            "es": "Tu codigo de verificacion: %s\n\nValido 15 minutos." % code,
            "it": "Il tuo codice di verifica: %s\n\nValido 15 minuti." % code}.get((lang or "en")[:2],
            "Your verification code: %s" % code)
    from django.core.mail import EmailMessage
    from django.conf import settings as st
    _safe_send(EmailMessage(subj, body, st.DEFAULT_FROM_EMAIL, [email]))


DIGITAL_STR = {
    "fr": {"subject": "Votre toile numérique « %s » est à vous", "hi": "Bonjour",
           "intro": "Merci pour votre achat ! Votre toile numérique est débloquée et rangée dans « Mes toiles ».",
           "paid": "Montant payé", "ref": "Référence", "play": "Peindre maintenant",
           "note": "Retrouvez-la à tout moment dans « Mes toiles » avec cette adresse e-mail. Ceci est votre confirmation d'achat ; le reçu de paiement Stripe vous est envoyé séparément.",
           "team": "L'équipe PaintIt"},
    "en": {"subject": "Your digital canvas “%s” is yours", "hi": "Hello",
           "intro": "Thank you for your purchase! Your digital canvas is unlocked and saved in “My canvases”.",
           "paid": "Amount paid", "ref": "Reference", "play": "Paint now",
           "note": "Find it anytime in “My canvases” with this e-mail address. This is your purchase confirmation; the Stripe payment receipt is sent separately.",
           "team": "The PaintIt team"},
    "de": {"subject": "Ihre digitale Leinwand „%s“ gehört Ihnen", "hi": "Hallo",
           "intro": "Danke für Ihren Kauf! Ihre digitale Leinwand ist freigeschaltet und in „Meine Leinwände“ gespeichert.",
           "paid": "Bezahlter Betrag", "ref": "Referenz", "play": "Jetzt malen",
           "note": "Sie finden sie jederzeit unter „Meine Leinwände“ mit dieser E-Mail-Adresse. Dies ist Ihre Kaufbestätigung; die Stripe-Zahlungsquittung erhalten Sie separat.",
           "team": "Ihr PaintIt-Team"},
    "es": {"subject": "Tu lienzo digital «%s» ya es tuyo", "hi": "Hola",
           "intro": "¡Gracias por tu compra! Tu lienzo digital está desbloqueado y guardado en «Mis lienzos».",
           "paid": "Importe pagado", "ref": "Referencia", "play": "Pintar ahora",
           "note": "Encuéntralo cuando quieras en «Mis lienzos» con esta dirección de correo. Esta es tu confirmación de compra; el recibo de pago de Stripe se envía por separado.",
           "team": "El equipo PaintIt"},
    "it": {"subject": "La tua tela digitale «%s» è tua", "hi": "Ciao",
           "intro": "Grazie per l'acquisto! La tua tela digitale è sbloccata e salvata in «Le mie tele».",
           "paid": "Importo pagato", "ref": "Riferimento", "play": "Dipingi ora",
           "note": "Ritrovala quando vuoi in «Le mie tele» con questo indirizzo e-mail. Questa è la conferma d'acquisto; la ricevuta di pagamento Stripe viene inviata a parte.",
           "team": "Il team PaintIt"},
}


def send_digital_receipt(email, uid, title, amount, lang="fr", cur="EUR"):
    """Confirmation d'achat d'une toile numerique (galerie ou deblocage DigiPaint)."""
    from django.conf import settings as st
    if not email:
        return
    T = DIGITAL_STR.get((lang or "fr")[:2], DIGITAL_STR["en"])
    site = st.SITE_URL
    logo = site + st.STATIC_URL + "studio/img/logo.png"
    name = title or uid
    play = "%s/paint/%s/" % (site, uid)
    thumb = "%s/preview/img/%s/preview/" % (site, uid)
    from .currency import fmt as _fmt
    price = _fmt(amount, (cur or "EUR").upper(), lang)
    text = "%s,\n\n%s\n\n%s\n%s : %s\n%s : %s\n%s : %s\n\n%s\n\n%s" % (
        T["hi"], T["intro"], name, T["paid"], price, T["ref"], uid, T["play"], play, T["note"], T["team"])
    html = (
        '<div style="background:#eef2f9;padding:24px 0;font-family:Arial,Helvetica,sans-serif">'
        '<table align="center" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;'
        'background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 6px 24px rgba(18,34,79,.08)">'
        '<tr><td style="background:#12224f;padding:18px 24px"><img src="%s" alt="PaintIt" height="26" '
        'style="height:26px;background:#fff;border-radius:8px;padding:5px 8px"></td></tr>'
        '<tr><td style="padding:22px 28px 6px;color:#12224f"><p style="margin:0 0 4px"><b>%s,</b></p>'
        '<p style="margin:0;color:#5b647a">%s</p></td></tr>'
        '<tr><td style="padding:12px 28px"><table width="100%%" cellpadding="0" cellspacing="0"><tr>'
        '<td style="width:96px"><img src="%s" width="88" style="width:88px;border-radius:10px;border:1px solid #e2e8f2"></td>'
        '<td style="padding-left:14px;color:#12224f;font-size:14px"><b style="font-size:16px">%s</b><br>'
        '<span style="color:#5b647a">%s : </span><b>%s</b><br><span style="color:#5b647a">%s : </span><code>%s</code></td>'
        '</tr></table></td></tr>'
        '<tr><td style="padding:8px 28px 4px"><a href="%s" style="display:inline-block;background:#2f6bf2;color:#fff;'
        'font-weight:700;text-decoration:none;padding:12px 22px;border-radius:999px">%s &rarr;</a></td></tr>'
        '<tr><td style="padding:14px 28px 22px;color:#5b647a;font-size:13px">%s<br><br>'
        '<b style="color:#2f6bf2">%s</b></td></tr></table></div>'
    ) % (logo, T["hi"], T["intro"], thumb, name, T["paid"], price, T["ref"], uid, play, T["play"], T["note"], T["team"])
    msg = EmailMultiAlternatives(T["subject"] % name, text, st.DEFAULT_FROM_EMAIL, [email])
    msg.attach_alternative(_brand(html), "text/html")
    _safe_send(msg)
