"""Codes de reduction : fidelite (usage unique) et promo (multi-usage).

- Fidelite : genere apres chaque commande (QR du poster), usage unique, taux = grille.
- Promo : cree en admin, pourcentage libre, nombre d'utilisations limite (ou illimite).

La consommation est une UPDATE conditionnelle atomique (compteur ou statut),
ce qui garantit le respect de la limite meme en cas d'acces concurrents.
"""
from django.db.models import F
from django.utils import timezone

from .models import Discount, Pricing


def norm(code):
    return (code or "").strip().upper()


def get_row(code):
    return Discount.objects.filter(code=norm(code)).first()


def issue(code):
    """Code PHYSIQUE (QR du poster) : parrainage via produit physique, usage unique."""
    code = norm(code)
    if not code:
        return
    pct = int(round(Pricing.get().referral_physical_rate * 100))
    Discount.objects.get_or_create(code=code, defaults={
        "kind": Discount.LOYALTY, "percent": pct, "max_uses": 1,
        "status": Discount.ISSUED, "active": True})


def issue_referral(code):
    """Code de PARRAINAGE digital a partager (taux referral_rate), USAGE UNIQUE.
    (Il etait cree en promo illimitee : un seul code partage pouvait servir a l'infini.)"""
    code = norm(code)
    if not code:
        return
    pct = int(round(Pricing.get().referral_rate * 100))
    Discount.objects.get_or_create(code=code, defaults={
        "kind": Discount.LOYALTY, "percent": pct, "max_uses": 1,
        "status": Discount.ISSUED, "active": True})


def status(code):
    row = get_row(code)
    if not row:
        return "unknown"
    if row.kind == Discount.PROMO:
        if not row.active:
            return "inactive"
        return "issued" if (row.max_uses == 0 or row.used_count < row.max_uses) else "used"
    return row.status


def is_redeemable(code):
    row = get_row(code)
    if not row or not row.active:
        return False
    if row.kind == Discount.PROMO:
        return row.max_uses == 0 or row.used_count < row.max_uses
    return row.status == Discount.ISSUED


def rate_for(code):
    row = get_row(code)
    return (row.percent or 0) / 100.0 if row else 0.0


def redeem(code, by=None):
    """Consomme le code. True si consomme, False sinon."""
    row = get_row(code)
    if not row or not row.active:
        return False
    if row.kind == Discount.PROMO:
        if row.max_uses == 0:
            Discount.objects.filter(pk=row.pk).update(used_count=F("used_count") + 1)
            return True
        return Discount.objects.filter(pk=row.pk, used_count__lt=row.max_uses)\
            .update(used_count=F("used_count") + 1) == 1
    return Discount.objects.filter(code=norm(code), status=Discount.ISSUED)\
        .update(status=Discount.USED, used_by=by, used_at=timezone.now()) == 1
