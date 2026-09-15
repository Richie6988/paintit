from django.db import migrations


def seed(apps, schema_editor):
    KitFormat = apps.get_model("studio", "KitFormat")
    Pricing = apps.get_model("studio", "Pricing")
    p = Pricing.objects.first()
    defaults = [
        (30, 40, getattr(p, "p_30x40", 24.90) if p else 24.90, 0),
        (40, 40, 29.90, 1),
        (40, 50, getattr(p, "p_40x50", 34.90) if p else 34.90, 2),
    ]
    for w, h, price, sort in defaults:
        KitFormat.objects.get_or_create(width_cm=w, height_cm=h,
                                        defaults={"price": price, "available": True, "sort": sort})


def unseed(apps, schema_editor):
    apps.get_model("studio", "KitFormat").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("studio", "0016_kitformat")]
    operations = [migrations.RunPython(seed, unseed)]
