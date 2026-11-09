"""Codes imprimes sur les posters (= n° de commande) : ils etaient crees au taux « parrainage physique » (40 %)
alors que le poster annonce la remise fidelite (15 %). Les codes pas encore utilises passent au taux fidelite."""
from django.db import migrations


def forward(apps, schema_editor):
    Discount = apps.get_model("studio", "Discount")
    Order = apps.get_model("studio", "Order")
    Pricing = apps.get_model("studio", "Pricing")
    p = Pricing.objects.filter(pk=1).first()
    pct = int(round((p.discount_rate if p else 0.15) * 100))
    uids = {u.strip().upper() for u in Order.objects.values_list("uid", flat=True)}
    for d in Discount.objects.filter(kind="loyalty", status="issued", used_count=0).exclude(percent=pct):
        if d.code in uids:
            Discount.objects.filter(pk=d.pk).update(percent=pct)


class Migration(migrations.Migration):
    dependencies = [("studio", "0077_yingsa_grid_2026")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
