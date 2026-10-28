from django.db import migrations, models

FK_SIZES = {"30x40", "40x40", "40x50"}          # devis Yingsa 2026 : DIY frame
NK_SIZES = {"20x20", "30x40", "30x50"}          # devis Yingsa 2026 : framed kit


def forward(apps, schema_editor):
    Order = apps.get_model("studio", "Order")
    Order.objects.filter(framed=True).update(finish="nk")
    KitFormat = apps.get_model("studio", "KitFormat")
    for kf in KitFormat.objects.all():
        a, b = sorted((kf.width_cm, kf.height_cm))
        key = "%dx%d" % (a, b)
        kf.nk_price = kf.frame_price
        kf.fk_available = key in FK_SIZES
        kf.nk_available = key in NK_SIZES
        kf.save(update_fields=["nk_price", "fk_available", "nk_available"])


def backward(apps, schema_editor):
    apps.get_model("studio", "Order").objects.filter(finish__in=("fk", "nk")).update(framed=True)


class Migration(migrations.Migration):
    dependencies = [("studio", "0075_order_currency")]

    operations = [
        migrations.AddField("order", "finish", models.CharField(
            "Finition", max_length=2, default="ck",
            choices=[("ck", "Sans cadre (CK)"), ("fk", "Cadre à monter (FK)"), ("nk", "Encadrée (NK)")])),
        migrations.AddField("kitformat", "fk_price", models.DecimalField(
            "Supplément cadre à monter (EUR)", max_digits=7, decimal_places=2, default=9.90)),
        migrations.AddField("kitformat", "fk_available", models.BooleanField("Cadre à monter disponible", default=False)),
        migrations.AddField("kitformat", "nk_price", models.DecimalField(
            "Supplément encadrée (EUR)", max_digits=7, decimal_places=2, default=12.00)),
        migrations.AddField("kitformat", "nk_available", models.BooleanField("Encadrée disponible", default=False)),
        migrations.RunPython(forward, backward),
        migrations.RemoveField("order", "framed"),
        migrations.RemoveField("kitformat", "frame_price"),
        migrations.RemoveField("kitformat", "frame_available"),
    ]
