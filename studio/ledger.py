"""Comptabilite : grand livre (LedgerEntry) alimente par Stripe et les reglements fournisseurs, synthese mensuelle,
exports pour l'expert-comptable (journal des ventes, ecritures au format FEC, factures PDF, achats).

Sources d'ecritures :
  - vente      : webhook / retour Stripe (kit, toile numerique, modele galerie) ; rattrapage depuis les commandes ;
  - frais      : frais Stripe de chaque paiement (rapprochement BalanceTransaction, cron `ledger_sync`) ;
  - rembours.  : webhook charge.refunded + rapprochement Stripe ;
  - virement   : virements Stripe -> banque (payouts) ;
  - fournisseur: confirmation d'un reglement (SupplierPayment).
Montants TTC en EUR ; TVA calculee au taux de CompanyInfo (0 si franchise en base).
"""
import csv
import datetime
import io
import logging

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)
EU = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV",
      "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}           # hors France
OSS_THRESHOLD = 10000.0      # ventes B2C a distance intra-UE (seuil annuel, au-dela : TVA du pays du client)
LABELS = {"kit": "Kit peinture par numéros", "digital": "Toile numérique DigiPaint", "gallery": "Modèle galerie"}
ACCOUNTS = {"stripe": ("580000", "Stripe (fonds en transit)"), "bank": ("512000", "Banque"),
            "kit": ("707000", "Ventes de marchandises"), "digital": ("706000", "Prestations de services"),
            "gallery": ("706000", "Prestations de services"), "vat": ("445710", "TVA collectée"),
            "fee": ("627800", "Frais de paiement"), "purchase": ("607000", "Achats de marchandises"),
            "supplier": ("401000", "Fournisseurs")}


def vat_rate():
    from .models import CompanyInfo
    return float(CompanyInfo.get().vat_rate or 0)


def vat_of(amount, rate=None):
    r = vat_rate() if rate is None else rate
    return round(amount - amount / (1 + r / 100.0), 2) if r else 0.0


def _country(order):
    from . import address
    if not order:
        return ""
    return ((address.COUNTRIES.get(order.country) or (order.country[:2].upper() if order.country else "",))[0] or "")[:2]


def _create(**kw):
    from .models import LedgerEntry, stripe_test_mode
    order = kw.get("order")
    kw.setdefault("is_test", bool(order.is_test) if order else stripe_test_mode())
    try:
        return LedgerEntry.objects.create(**kw)
    except IntegrityError:          # deja enregistre (webhook + retour navigateur + rapprochement)
        return None


# ---------------------------------------------------------------- enregistrement
def record_sale(kind, uid, amount, date=None, stripe_id="", email="", order=None):
    """Encaissement d'une vente (idempotent). -> LedgerEntry ou None si deja present."""
    from .models import LedgerEntry, Order
    if kind == "kit" and not order and uid:
        order = Order.objects.filter(uid=uid).first()
    if stripe_id and LedgerEntry.objects.filter(kind="sale", stripe_id=stripe_id).exists():
        return None
    if order and LedgerEntry.objects.filter(kind="sale", order=order).exists():
        e = LedgerEntry.objects.filter(kind="sale", order=order, stripe_id="").first()
        if e and stripe_id:                        # rattrapage puis Stripe : on complete l'identifiant
            LedgerEntry.objects.filter(pk=e.pk).update(stripe_id=stripe_id)
        return None
    amount = round(float(amount or 0), 2)
    return _create(date=date or timezone.now(), kind="sale", source=kind if kind in LABELS else "kit", order=order,
                   uid=uid or "", label=LABELS.get(kind, "Vente") + (" %s" % uid if uid else ""),
                   reference=(order.invoice_number if order else "") or (uid or "")[:40],
                   email=(email or (order.customer_email if order else ""))[:254], country=_country(order),
                   amount=amount, vat=vat_of(amount), stripe_id=stripe_id or "")


def record_from_session(sess):
    """Session Checkout Stripe payee -> vente."""
    try:
        meta = sess.get("metadata") or {}
        det = sess.get("customer_details") or {}
        from .currency import to_eur, from_minor    # paiement en devise : compta en EUR (taux BCE du jour)
        return record_sale(meta.get("kind", "kit"), meta.get("uid") or sess.get("client_reference_id") or "",
                           to_eur(from_minor(sess.get("amount_total"), sess.get("currency")), sess.get("currency")),
                           stripe_id=sess.get("payment_intent") or "",
                           email=det.get("email") or sess.get("customer_email") or meta.get("email") or "",
                           date=datetime.datetime.fromtimestamp(sess["created"], datetime.timezone.utc)
                           if sess.get("created") else None)
    except Exception:
        logger.exception("Grand livre : session Stripe")
        return None


def record_refund(refund_id, amount, uid="", kind="kit", date=None, payment_intent=""):
    """Remboursement (montant positif en entree, ecrit en negatif)."""
    from .models import Order, LedgerEntry
    order = Order.objects.filter(uid=uid).first() if uid and kind == "kit" else None
    amount = round(abs(float(amount or 0)), 2)
    if not amount:
        return None
    sale = LedgerEntry.objects.filter(kind="sale", stripe_id=payment_intent).first() if payment_intent else None
    e = _create(date=date or timezone.now(), kind="refund", source=kind if kind in LABELS else "kit", order=order,
                uid=uid or "", label="Remboursement %s" % (uid or ""),
                reference=("AV-" + order.invoice_number) if order and order.invoice_number else (uid or "")[:40],
                email=order.customer_email if order else (sale.email if sale else ""), country=_country(order),
                amount=-amount, vat=-vat_of(amount), stripe_id=refund_id or "")
    if e and order:
        total = -(LedgerEntry.objects.filter(kind="refund", order=order).aggregate(s=Sum("amount"))["s"] or 0)
        Order.objects.filter(pk=order.pk).update(refunded_amount=round(total, 2))
    return e


def record_supplier_payment(pay):
    return _create(date=timezone.make_aware(datetime.datetime.combine(pay.paid_at or timezone.localdate(),
                                                                      datetime.time(12))),
                   kind="supplier", source="supplier", supplier_payment=pay,
                   label="Règlement %s" % pay.supplier.name, reference=pay.reference[:40],
                   amount=-round(pay.amount_eur, 2), stripe_id="")


# ---------------------------------------------------------------- rapprochement
def sync_orders():
    """Commandes payees sans ecriture de vente (paiement anterieur au grand livre, webhook manque)."""
    from .models import LedgerEntry, Order
    done = LedgerEntry.objects.filter(kind="sale").exclude(order=None).values("order_id")
    n = 0
    for o in Order.objects.filter(status__in=Order.PAID_STATUSES).exclude(pk__in=done):
        if record_sale("kit", o.uid, o.total, date=o.created_at, order=o):
            n += 1
    return n


def sync_stripe(days=40):
    """Rapproche le solde Stripe : ventes manquantes, frais reels, remboursements, virements vers la banque."""
    if not settings.STRIPE_SECRET_KEY:
        return {"skipped": "Stripe non configuré"}
    import stripe
    from .models import LedgerEntry
    stripe.api_key = settings.STRIPE_SECRET_KEY
    since = int((timezone.now() - datetime.timedelta(days=days)).timestamp())
    out = {"sales": 0, "fees": 0, "refunds": 0, "payouts": 0}
    bts = stripe.BalanceTransaction.list(created={"gte": since}, limit=100, expand=["data.source"])
    from .payments import plain
    for bt in bts.auto_paging_iter():
        bt = plain(bt)
        when = datetime.datetime.fromtimestamp(bt["created"], datetime.timezone.utc)
        src = bt.get("source") or {}
        typ = bt["type"]
        try:
            if typ in ("charge", "payment"):
                pi_id = src.get("payment_intent") if isinstance(src, dict) else ""
                meta = {}
                if pi_id:
                    meta = (plain(stripe.PaymentIntent.retrieve(pi_id)).get("metadata") or {})
                if record_sale(meta.get("kind", "kit"), meta.get("uid", ""), bt["amount"] / 100.0, date=when,
                               stripe_id=pi_id or src.get("id", ""),
                               email=((src.get("billing_details") or {}).get("email") or "") if isinstance(src, dict) else ""):
                    out["sales"] += 1
                fee = round(bt["fee"] / 100.0, 2)
                n = LedgerEntry.objects.filter(kind="sale", stripe_id=pi_id or src.get("id", "")).exclude(fee=fee).update(fee=fee)
                out["fees"] += n
            elif typ in ("refund", "payment_refund"):
                pi_id = src.get("payment_intent") if isinstance(src, dict) else ""
                meta = (plain(stripe.PaymentIntent.retrieve(pi_id)).get("metadata") or {}) if pi_id else {}
                if record_refund(src.get("id", bt["id"]), -bt["amount"] / 100.0, uid=meta.get("uid", ""),
                                 kind=meta.get("kind", "kit"), date=when, payment_intent=pi_id or ""):
                    out["refunds"] += 1
            elif typ == "payout":
                if _create(date=when, kind="payout", source="stripe", label="Virement Stripe vers la banque",
                           reference=(src.get("id") if isinstance(src, dict) else bt["id"])[:40],
                           amount=round(-bt["amount"] / 100.0, 2), stripe_id=bt["id"]):
                    out["payouts"] += 1
            elif typ in ("stripe_fee", "application_fee", "adjustment") and bt["amount"] < 0:
                _create(date=when, kind="fee", source="stripe", label=bt.get("description") or "Frais Stripe",
                        reference=bt["id"][:40], amount=0.0, fee=round(-bt["amount"] / 100.0, 2), stripe_id=bt["id"])
        except Exception:
            logger.exception("Rapprochement Stripe %s", bt.get("id"))
    return out


# ---------------------------------------------------------------- synthese
def month_bounds(ym):
    try:
        y, m = [int(x) for x in (ym or "").split("-")]
        start = datetime.date(y, m, 1)
    except (ValueError, TypeError):
        t = timezone.localdate()
        start = t.replace(day=1)
    end = (start + datetime.timedelta(days=32)).replace(day=1)
    tz = timezone.get_current_timezone()
    return start, timezone.make_aware(datetime.datetime.combine(start, datetime.time()), tz), \
        timezone.make_aware(datetime.datetime.combine(end, datetime.time()), tz)


def entries(start, end):
    from .models import LedgerEntry
    return LedgerEntry.real.filter(date__gte=start, date__lt=end).select_related("order", "supplier_payment")


def summary(start, end):
    from .models import Order, SupplierPayment
    qs = entries(start, end)
    agg = lambda q, f="amount": round(q.aggregate(s=Sum(f))["s"] or 0.0, 2)
    sales = qs.filter(kind="sale")
    by_src = {k: agg(sales.filter(source=k)) for k in LABELS}
    ttc = agg(sales)
    refunds = agg(qs.filter(kind="refund"))
    vat = round(agg(sales, "vat") + agg(qs.filter(kind="refund"), "vat"), 2)
    fees = round(agg(qs, "fee"), 2)
    purchases = round(Order.real.filter(status__in=Order.PAID_STATUSES, created_at__gte=start, created_at__lt=end)
                      .aggregate(s=Sum("cost"))["s"] or 0.0, 2)
    paid_sup = -agg(qs.filter(kind="supplier"))
    ht = round(ttc + refunds - vat, 2)
    missing_fee = sales.filter(fee=0, stripe_id__startswith="pi_").count()
    unrecorded = Order.real.filter(status__in=Order.PAID_STATUSES, created_at__gte=start, created_at__lt=end) \
        .exclude(ledger__kind="sale").count()
    return {"ttc": ttc, "by_src": by_src, "refunds": refunds, "vat": vat, "ht": ht, "fees": fees,
            "purchases": purchases, "paid_suppliers": paid_sup, "margin": round(ht - purchases - fees, 2),
            "margin_pct": round((ht - purchases - fees) / ht * 100, 1) if ht else None,
            "payouts": agg(qs.filter(kind="payout")), "n_sales": sales.count(),
            "missing_fee": missing_fee, "unrecorded": unrecorded,
            "drafts": SupplierPayment.objects.filter(status="draft").count()}


def oss_status(year=None):
    """Ventes a distance B2C vers d'autres pays de l'UE depuis le 1er janvier (seuil OSS 10 000 EUR)."""
    from .models import LedgerEntry
    year = year or timezone.localdate().year
    qs = LedgerEntry.real.filter(date__year=year, kind__in=("sale", "refund"), country__in=EU)
    total = round(qs.aggregate(s=Sum("amount"))["s"] or 0.0, 2)
    return {"year": year, "total": total, "threshold": OSS_THRESHOLD, "pct": min(100, round(total / OSS_THRESHOLD * 100)),
            "over": total >= OSS_THRESHOLD, "near": total >= OSS_THRESHOLD * 0.8}


# ---------------------------------------------------------------- exports
def _num(x):
    return ("%.2f" % x).replace(".", ",")


def sales_csv(start, end):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Date", "Pièce", "Type", "Produit", "Client", "Pays", "HT", "TVA", "TTC", "Frais Stripe", "Net encaissé",
                "Réf. Stripe"])
    for e in entries(start, end).filter(kind__in=("sale", "refund")).order_by("date"):
        ref = (e.order.invoice_number if e.order and e.kind == "sale" and e.order.invoice_number else e.reference)
        w.writerow([timezone.localtime(e.date).strftime("%d/%m/%Y"), ref, e.get_kind_display(), e.get_source_display(),
                    e.email, e.country, _num(e.amount - e.vat), _num(e.vat), _num(e.amount), _num(e.fee), _num(e.net),
                    e.stripe_id])
    return "﻿" + buf.getvalue()


def purchases_csv(start, end):
    from .models import Order
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Date commande", "Commande", "Facture client", "Fournisseur", "Produit", "Coût (EUR)", "Réglé le",
                "Réf. règlement"])
    for o in Order.real.filter(status__in=Order.PAID_STATUSES, created_at__gte=start, created_at__lt=end) \
            .select_related("supplier").order_by("created_at"):
        w.writerow([timezone.localtime(o.created_at).strftime("%d/%m/%Y"), o.uid, o.invoice_number,
                    o.supplier.name if o.supplier else "", "%s · %d couleurs" % (o.format_label, o.colors),
                    _num(o.cost or 0), o.supplier_paid_at.strftime("%d/%m/%Y") if o.supplier_paid_at else "",
                    o.supplier_payment_ref])
    return "﻿" + buf.getvalue()


def fec_lines(start, end):
    """Ecritures en partie double (colonnes du Fichier des Ecritures Comptables), a importer par l'expert-comptable."""
    head = ["JournalCode", "JournalLib", "EcritureNum", "EcritureDate", "CompteNum", "CompteLib", "CompAuxNum",
            "CompAuxLib", "PieceRef", "PieceDate", "EcritureLib", "Debit", "Credit", "EcritureLet", "DateLet",
            "ValidDate", "Montantdevise", "Idevise"]
    rows = [head]
    n = 0
    for e in entries(start, end).order_by("date", "pk"):
        n += 1
        d = timezone.localtime(e.date).strftime("%Y%m%d")
        ref = (e.order.invoice_number if e.order and e.order.invoice_number and e.kind == "sale" else e.reference) or str(e.pk)
        lib = (e.label or e.get_kind_display())[:100]
        lines = []

        def L(acc, debit, credit, aux=("", "")):
            if round(debit, 2) or round(credit, 2):
                lines.append([acc[0], acc[1], aux[0], aux[1], _num(debit), _num(credit)])
        if e.kind in ("sale", "refund"):
            amt, vat = abs(e.amount), abs(e.vat)
            prod = ACCOUNTS.get(e.source, ACCOUNTS["kit"])
            if e.kind == "sale":
                L(ACCOUNTS["stripe"], amt, 0); L(prod, 0, amt - vat); L(ACCOUNTS["vat"], 0, vat)
            else:
                L(prod, amt - vat, 0); L(ACCOUNTS["vat"], vat, 0); L(ACCOUNTS["stripe"], 0, amt)
            if e.fee:
                L(ACCOUNTS["fee"], e.fee, 0); L(ACCOUNTS["stripe"], 0, e.fee)
            jc, jl = "VT", "Ventes"
        elif e.kind == "fee":
            L(ACCOUNTS["fee"], e.fee, 0); L(ACCOUNTS["stripe"], 0, e.fee)
            jc, jl = "BQ", "Banque"
        elif e.kind == "payout":
            L(ACCOUNTS["bank"], e.amount, 0); L(ACCOUNTS["stripe"], 0, e.amount)
            jc, jl = "BQ", "Banque"
        else:   # reglement fournisseur
            sup = e.supplier_payment.supplier.name if e.supplier_payment else ""
            aux = ("F%04d" % e.supplier_payment.supplier_id, sup) if e.supplier_payment else ("", "")
            L(ACCOUNTS["purchase"], -e.amount, 0); L(ACCOUNTS["supplier"], 0, -e.amount, aux)
            jc, jl = "AC", "Achats"
        for acc, accl, auxn, auxl, deb, cre in lines:
            rows.append([jc, jl, str(n), d, acc, accl, auxn, auxl, ref, d, lib, deb, cre, "", "", d, "", ""])
    return "\n".join("\t".join(r) for r in rows) + "\n"


def invoices_zip(start, end):
    """PDF des factures des commandes payees du mois."""
    import zipfile
    from .models import Order
    from . import receipts
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for o in Order.real.filter(status__in=Order.PAID_STATUSES, created_at__gte=start, created_at__lt=end) \
                .order_by("invoice_number", "created_at"):
            try:
                z.writestr("%s_%s.pdf" % (o.invoice_number or "sans-numero", o.uid), receipts.build_receipt(o))
            except Exception:
                logger.exception("Facture %s", o.uid)
    return buf.getvalue()


# ---------------------------------------------------------------- reglements fournisseurs
def sepa_eligible(sup):
    iban = (sup.iban or "").replace(" ", "").upper()
    from .supplier import EU as _EU
    return len(iban) >= 15 and (iban[:2] in _EU or iban[:2] in ("FR", "MC", "CH", "GB", "NO", "IS", "LI"))


def sepa_xml(pay):
    """Ordre de virement SEPA (pain.001.001.03) a importer dans la banque de l'entreprise."""
    from xml.sax.saxutils import escape
    from .models import CompanyInfo
    c = CompanyInfo.get()
    sup = pay.supplier
    now = timezone.now().strftime("%Y-%m-%dT%H:%M:%S")
    amt = "%.2f" % pay.amount_eur
    iban = (sup.iban or "").replace(" ", "").upper()
    bic = (sup.bic or "").replace(" ", "").upper()
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"><CstmrCdtTrfInitn>'
            '<GrpHdr><MsgId>%(ref)s</MsgId><CreDtTm>%(now)s</CreDtTm><NbOfTxs>1</NbOfTxs><CtrlSum>%(amt)s</CtrlSum>'
            '<InitgPty><Nm>%(dn)s</Nm></InitgPty></GrpHdr>'
            '<PmtInf><PmtInfId>%(ref)s</PmtInfId><PmtMtd>TRF</PmtMtd><NbOfTxs>1</NbOfTxs><CtrlSum>%(amt)s</CtrlSum>'
            '<PmtTpInf><SvcLvl><Cd>SEPA</Cd></SvcLvl></PmtTpInf><ReqdExctnDt>%(day)s</ReqdExctnDt>'
            '<Dbtr><Nm>%(dn)s</Nm></Dbtr><DbtrAcct><Id><IBAN>%(diban)s</IBAN></Id></DbtrAcct>'
            '<DbtrAgt><FinInstnId>%(dbic)s</FinInstnId></DbtrAgt><ChrgBr>SLEV</ChrgBr>'
            '<CdtTrfTxInf><PmtId><EndToEndId>%(ref)s</EndToEndId></PmtId><Amt><InstdAmt Ccy="EUR">%(amt)s</InstdAmt></Amt>'
            '%(cbic)s<Cdtr><Nm>%(cn)s</Nm></Cdtr><CdtrAcct><Id><IBAN>%(ciban)s</IBAN></Id></CdtrAcct>'
            '<RmtInf><Ustrd>%(ref)s</Ustrd></RmtInf></CdtTrfTxInf></PmtInf></CstmrCdtTrfInitn></Document>\n') % {
        "ref": escape(pay.reference[:35]), "now": now, "amt": amt, "day": timezone.localdate().isoformat(),
        "dn": escape((c.legal_name or "PaintIt")[:70]), "diban": escape((c.bank_iban or "").replace(" ", "").upper()),
        "dbic": ("<BIC>%s</BIC>" % escape(c.bank_bic.replace(" ", "").upper())) if c.bank_bic else "<Othr><Id>NOTPROVIDED</Id></Othr>",
        "cbic": ("<CdtrAgt><FinInstnId><BIC>%s</BIC></FinInstnId></CdtrAgt>" % escape(bic)) if bic else "",
        "cn": escape(sup.name[:70]), "ciban": escape(iban)}


def prepare_payment(sup, orders, user=""):
    """Prepare un reglement (brouillon) pour les commandes donnees. -> SupplierPayment"""
    from .models import Order, SupplierPayment
    orders = [o for o in orders if not o.supplier_paid_at and o.supplier_payment_id is None]
    if not orders:
        return None
    eur = round(sum(float(o.cost or 0) for o in orders), 2)
    g = sup.grid()
    cur = (g.get("currency") or "EUR") if g else "EUR"
    native = round(eur / sup.grid_fx(), 2) if cur != "EUR" else eur
    n = SupplierPayment.objects.filter(supplier=sup).count() + 1
    ref = "PAINTIT-%s-%s-%02d" % ("".join(ch for ch in sup.name.upper() if ch.isalnum())[:8] or "SUP",
                                  timezone.localdate().strftime("%y%m%d"), n)
    pay = SupplierPayment.objects.create(supplier=sup, reference=ref[:40], amount_eur=eur, currency=cur,
                                         amount_native=native, created_by=user[:80])
    Order.objects.filter(pk__in=[o.pk for o in orders]).update(supplier_payment=pay)
    return pay


def confirm_payment(pay, paid_at=None, bank_ref=""):
    from .models import Order
    if pay.status == pay.SENT:
        return pay
    pay.status, pay.paid_at, pay.bank_ref = pay.SENT, paid_at or timezone.localdate(), bank_ref[:80]
    pay.save(update_fields=["status", "paid_at", "bank_ref"])
    Order.objects.filter(supplier_payment=pay).update(
        supplier_paid_at=timezone.make_aware(datetime.datetime.combine(pay.paid_at, datetime.time(12))),
        supplier_payment_ref=pay.reference)
    record_supplier_payment(pay)
    return pay


def cancel_payment(pay):
    from .models import Order
    if pay.status == pay.SENT:
        return False
    Order.objects.filter(supplier_payment=pay).update(supplier_payment=None)
    pay.delete()
    return True
