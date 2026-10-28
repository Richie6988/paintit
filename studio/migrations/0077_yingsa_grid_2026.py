"""Devis Yingsa Arts 2026 : remplace la grille tarifaire des fournisseurs dont le devis suit ce modele
(produits unframed / frame...), en gardant leur taux de change."""
import copy

from django.db import migrations

GRID_2026 = {
    "currency": "USD", "fx": 0.92, "cols": [24],
    "zones": [{"key": "eu", "label": "Europe (NL/FR…)", "countries": "EU", "days": "6-15", "fee": 6},
              {"key": "uk", "label": "UK", "countries": "GB", "days": "6-15"},
              {"key": "us", "label": "USA", "countries": "US", "days": "6-15"}],
    "products": [{"key": "unframed", "label": "Unframe kit (CK)", "kit": True},
                 {"key": "diy_frame", "label": "DIY frame (FK)", "kit": False},
                 {"key": "framed", "label": "Framed kit (NK)", "kit": False}],
    "rows": [{"product": prod, "size": sz, "on": True, "price": {"24": unit}, "ship": {"eu": eu, "uk": uk, "us": us}}
             for prod, sz, unit, eu, uk, us in (
                 ("unframed", "30x40", 4.5, 10.0, 8.0, 12.0), ("unframed", "40x40", 5.5, 12.0, 9.5, 14.0),
                 ("unframed", "40x50", 6.5, 12.5, 10.0, 15.0), ("unframed", "40x60", 7.0, 13.5, 11.0, 16.0),
                 ("unframed", "50x50", 7.5, 13.5, 11.5, 16.0), ("unframed", "50x60", 8.0, 15.0, 12.0, 18.0),
                 ("unframed", "50x70", 9.0, 16.0, 13.0, 20.0), ("unframed", "50x75", 9.5, 17.0, 14.0, 22.0),
                 ("diy_frame", "30x40", 7.0, 12.0, 10.0, 15.0), ("diy_frame", "40x40", 7.5, 14.0, 12.0, 16.0),
                 ("diy_frame", "40x50", 8.0, 17.0, 14.0, 20.0),
                 ("framed", "20x20", 6.5, 13.0, 11.0, 16.0), ("framed", "30x40", 7.5, 15.0, 12.0, 18.0),
                 ("framed", "30x50", 8.0, 17.0, 13.0, 20.0))],
}


def forward(apps, schema_editor):
    Supplier = apps.get_model("studio", "Supplier")
    for sup in Supplier.objects.all():
        offer = dict(sup.offer or {})
        old = offer.get("grid") or {}
        keys = {p.get("key") for p in old.get("products") or []}
        if not ("unframed" in keys or "yingsa" in (sup.name or "").lower()):
            continue
        new = copy.deepcopy(GRID_2026)
        if old.get("currency") == new["currency"] and old.get("fx"):
            new["fx"] = old["fx"]                   # taux de change saisi par l'utilisateur
        offer["grid_previous"] = old                # l'ancien devis reste consultable / restaurable
        offer["grid"] = new
        sup.offer = offer
        sup.save(update_fields=["offer"])


def backward(apps, schema_editor):
    Supplier = apps.get_model("studio", "Supplier")
    for sup in Supplier.objects.all():
        offer = dict(sup.offer or {})
        if "grid_previous" in offer:
            offer["grid"] = offer.pop("grid_previous")
            sup.offer = offer
            sup.save(update_fields=["offer"])


class Migration(migrations.Migration):
    dependencies = [("studio", "0076_kit_finishes")]
    operations = [migrations.RunPython(forward, backward)]
