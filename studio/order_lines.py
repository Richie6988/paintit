"""Contenu des commandes tel que le voit le fournisseur (MangoERP via le connecteur WooCommerce, Excel, API,
e-mail) : SKU BK/WF, variante, proprietes de ligne (fichiers), remarque courte, prix d'achat."""
from .models import supplier_color_count

PRODUCT_TITLE = "PaintIt - Peinture par numéros personnalisée"

FRAMED_PREFIX, UNFRAMED_PREFIX = "BK", "WF"      # codes du fournisseur : BK = kit avec cadre, WF = sans cadre


def sku_for(fmt_key, colors, framed=False):
    """SKU fournisseur : BK-40x50-24C (avec cadre) / WF-40x50-24C (sans cadre) ; 12 couleurs -> kit 24."""
    return "%s-%s-%dC" % (FRAMED_PREFIX if framed else UNFRAMED_PREFIX, fmt_key, supplier_color_count(colors))


def finish_label(framed):
    return "Framed / 带框 (BK)" if framed else "Unframed / 无框 (WF)"


def _split_name(full):
    parts = (full or "").strip().split()
    if len(parts) <= 1:
        return (parts[0] if parts else "Client"), "."
    return " ".join(parts[:-1]), parts[-1]


def _phone(code, number):
    n = "".join(ch for ch in (number or "") if ch.isdigit())
    c = "".join(ch for ch in (code or "") if ch.isdigit() or ch == "+")
    if not n:
        return None
    if c and not n.startswith(c.lstrip("+")):
        n = n.lstrip("0")
        return "+%s%s" % (c.lstrip("+"), n)
    return "+" + n


def variant_title(order):
    """Variante lisible par l'ERP : « 40x50 / 24 / WF »."""
    from .models import format_key
    return "%s / %d / %s" % (format_key(order.width_cm, order.height_cm), supplier_color_count(order.colors),
                             FRAMED_PREFIX if order.framed else UNFRAMED_PREFIX)


def supplier_amounts(order, sup=None):
    """Montants vus par le fournisseur : SON prix d'achat (grille/offre), jamais notre prix de vente.
    -> (toile, pinceaux, total) en EUR ; 0 si le format n'est pas tarife chez lui."""
    from .models import format_key, Supplier
    sup = sup or getattr(order, "supplier", None) or Supplier.for_checkout("kit")
    fmt = format_key(order.width_cm, order.height_cm)
    canvas = (sup.goods_cost(fmt, order.colors, False, bool(getattr(order, "framed", False))) if sup else None) or 0.0
    brushes = 0.0          # pinceaux : pas commandes au fournisseur (bonus PaintIt)
    return round(canvas, 2), brushes, round(canvas + brushes, 2)


def line_properties(order):
    """Proprietes de ligne de commande (vues par l'ERP du fournisseur), identiques dans le connecteur et l'Excel :
    les fichiers dont le fournisseur a besoin (+ ZIP de tout)."""
    from .security import file_url, production_url
    from .fulfillment import ensure_supplier_files
    u = order.uid
    ensure_supplier_files(u)
    return [("Commande PaintIt", u),
            ("All files / 全部文件", production_url(u)),
            ("Format", "%s (%s)" % (order.format_label, order.orientation)),
            ("Couleurs", str(supplier_color_count(order.colors))),
            ("Finish / 款式", finish_label(order.framed)),
            ("Poster (print, put in parcel / 打印放入包裹)", file_url(u, "%s_poster.png" % u)),
            ("Toile numérotée (TIFF)", file_url(u, "%s_template.tiff" % u)),
            ("Order sheet / 订单 (TXT)", file_url(u, "%s_supplier.txt" % u)),
            ("Colour table / 色卡 (JPG)", file_url(u, "%s_supplier_colors.jpg" % u)),
            ("Fiche couleurs (JSON)", file_url(u, "%s_supplier.json" % u)),
            ("Fichiers (ZIP)", file_url(u, "production.zip"))]


def _short_note(order):
    """Remarque courte (les ERP tronquent ou ignorent les remarques longues) : un seul lien vers tous les fichiers."""
    from .security import production_url
    return "PaintIt %s | %s, %s colors / 色, %s\nAll files / 全部文件: %s" % (
        order.uid, order.format_label, supplier_color_count(order.colors), finish_label(order.framed),
        production_url(order.uid))
