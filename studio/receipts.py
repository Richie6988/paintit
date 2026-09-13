"""Recu PDF de commande (reportlab), localise selon le pays de livraison,
avec un petit apercu de l'oeuvre."""
import io
import os

from django.conf import settings
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdfcanvas

from .fulfillment import lang_for_country

NAVY = (0.07, 0.13, 0.31)
BLUE = (0.18, 0.42, 0.95)
GREY = (0.36, 0.39, 0.48)

STR = {
    "fr": {"receipt": "Reçu de commande", "order": "Commande", "product": "Produit",
           "format": "Format", "dims": "Dimensions", "colors": "Couleurs",
           "brushes": "Pinceaux", "yes": "Oui", "no": "Non", "payment": "Paiement",
           "subtotal": "Sous-total", "discount": "Remise", "shipping": "Livraison",
           "free": "Offerte", "total": "Total", "delivery": "Livraison",
           "thanks": "Merci pour votre commande PaintIt.",
           "orient": {"portrait": "portrait", "paysage": "paysage"}},
    "en": {"receipt": "Order receipt", "order": "Order", "product": "Product",
           "format": "Format", "dims": "Dimensions", "colors": "Colors",
           "brushes": "Brushes", "yes": "Yes", "no": "No", "payment": "Payment",
           "subtotal": "Subtotal", "discount": "Discount", "shipping": "Shipping",
           "free": "Free", "total": "Total", "delivery": "Delivery",
           "thanks": "Thank you for your PaintIt order.",
           "orient": {"portrait": "portrait", "paysage": "landscape"}},
    "de": {"receipt": "Bestellbeleg", "order": "Bestellung", "product": "Produkt",
           "format": "Format", "dims": "Maße", "colors": "Farben",
           "brushes": "Pinsel", "yes": "Ja", "no": "Nein", "payment": "Zahlung",
           "subtotal": "Zwischensumme", "discount": "Rabatt", "shipping": "Versand",
           "free": "Kostenlos", "total": "Gesamt", "delivery": "Lieferung",
           "thanks": "Danke für Ihre PaintIt-Bestellung.",
           "orient": {"portrait": "Hochformat", "paysage": "Querformat"}},
    "es": {"receipt": "Recibo de pedido", "order": "Pedido", "product": "Producto",
           "format": "Formato", "dims": "Dimensiones", "colors": "Colores",
           "brushes": "Pinceles", "yes": "Sí", "no": "No", "payment": "Pago",
           "subtotal": "Subtotal", "discount": "Descuento", "shipping": "Envío",
           "free": "Gratis", "total": "Total", "delivery": "Entrega",
           "thanks": "Gracias por tu pedido PaintIt.",
           "orient": {"portrait": "vertical", "paysage": "horizontal"}},
}


def build_receipt(o):
    T = STR.get((o.lang or "en")[:2], STR["en"])
    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)
    W, H = A4
    x = 22 * mm
    y = H - 28 * mm

    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 24)
    c.drawString(x, y, "PaintIt")
    c.setFillColorRGB(*GREY); c.setFont("Helvetica", 10)
    c.drawRightString(W - 22 * mm, y, T["receipt"])
    y -= 6 * mm
    c.setStrokeColorRGB(*BLUE); c.setLineWidth(1.5); c.line(x, y, W - 22 * mm, y)
    y -= 12 * mm

    # Apercu de l'oeuvre (haut droite)
    prev = os.path.join(settings.MEDIA_ROOT, "orders", o.uid, f"{o.uid}_preview.png")
    img_bottom = y
    if os.path.exists(prev):
        img = ImageReader(prev)
        iw, ih = img.getSize()
        bw = 42 * mm
        bh = bw * ih / iw
        if bh > 55 * mm:                       # plafonne la hauteur
            bh = 55 * mm; bw = bh * iw / ih
        img_bottom = y - bh
        c.drawImage(img, W - 22 * mm - bw, img_bottom, bw, bh,
                    preserveAspectRatio=True, mask="auto")
        c.setStrokeColorRGB(*GREY); c.setLineWidth(0.5)
        c.rect(W - 22 * mm - bw, img_bottom, bw, bh, stroke=1, fill=0)

    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 12)
    c.drawString(x, y, f"{T['order']} {o.uid}"); y -= 6 * mm
    c.setFillColorRGB(*GREY); c.setFont("Helvetica", 10)
    c.drawString(x, y, o.created_at.strftime("%d/%m/%Y %H:%M")); y -= 10 * mm
    y = min(y, img_bottom - 8 * mm)             # demarre sous l'image

    def row(label, val, bold=False):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 11)
        c.setFillColorRGB(*(NAVY if bold else GREY)); c.drawString(x, y, label)
        c.setFillColorRGB(*NAVY); c.drawRightString(W - 22 * mm, y, val)
        y -= 7 * mm

    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 11); c.drawString(x, y, T["product"]); y -= 7 * mm
    row(T["format"], f"{o.format_label} ({T['orient'].get(o.orientation, o.orientation)})")
    row(T["dims"], f"{o.width_cm:g} x {o.height_cm:g} cm")
    row(T["colors"], f"{o.colors}")
    row(T["brushes"], T["yes"] if o.brushes else T["no"])
    y -= 3 * mm
    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 11); c.drawString(x, y, T["payment"]); y -= 7 * mm
    row(T["subtotal"], f"{round(o.price - o.brushes_amount, 2)} EUR")
    if o.brushes_amount:
        row(T["brushes"], f"+{o.brushes_amount} EUR")
    if o.discount_code:
        row(f"{T['discount']} ({o.discount_code})", f"-{o.discount_amount} EUR")
    row(T["shipping"], T["free"])
    row(T["total"], f"{o.total} EUR", bold=True)
    y -= 3 * mm
    c.setFillColorRGB(*NAVY); c.setFont("Helvetica-Bold", 11); c.drawString(x, y, T["delivery"]); y -= 7 * mm
    c.setFillColorRGB(*GREY); c.setFont("Helvetica", 10)
    for line in [o.customer_name, f"{o.address1} {o.address2}".strip(),
                 f"{o.postal_code} {o.city} ({o.country})",
                 f"{o.phone_code} {o.phone}".strip(), o.customer_email]:
        if line:
            c.drawString(x, y, line); y -= 6 * mm

    c.setFillColorRGB(*GREY); c.setFont("Helvetica", 8)
    c.drawString(x, 18 * mm, T["thanks"])
    c.showPage(); c.save()
    return buf.getvalue()
