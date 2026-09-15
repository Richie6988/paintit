from django.db import models


class Pricing(models.Model):
    """Grille tarifaire editable (tour de controle) :
    un prix par format, un supplement par palette de couleurs, le prix des pinceaux."""
    # Prix par format (EUR)
    p_30x40 = models.FloatField("30 x 40 cm", default=24.9)
    p_40x50 = models.FloatField("40 x 50 cm", default=34.9)
    p_50x70 = models.FloatField("50 x 70 cm", default=49.9)
    p_60x80 = models.FloatField("60 x 80 cm", default=64.9)
    p_70x100 = models.FloatField("70 x 100 cm", default=84.9)
    p_80x120 = models.FloatField("80 x 120 cm", default=109.9)
    # Supplement selon le nombre de couleurs (EUR)
    c_12 = models.FloatField("12 couleurs (+)", default=0.0)
    c_24 = models.FloatField("24 couleurs (+)", default=8.0)
    c_36 = models.FloatField("36 couleurs (+)", default=16.0)
    # Options
    brushes_price = models.FloatField("Set de pinceaux", default=3.0)
    # Remise & livraison
    discount_rate = models.FloatField("Taux de remise fidelite", default=0.15)
    referral_rate = models.FloatField("Parrainage (partage digital)", default=0.20)
    referral_physical_rate = models.FloatField("Parrainage via produit physique", default=0.40)
    free_shipping = models.BooleanField("Livraison offerte", default=True)
    delivery_days_min = models.IntegerField("Delai livraison min (jours)", default=5)
    delivery_days_max = models.IntegerField("Delai livraison max (jours)", default=9)
    # Couts dropshipping (pour la marge)
    cost_base = models.FloatField(default=6.0)
    cost_per_cm2 = models.FloatField(default=0.002)
    cost_per_color = models.FloatField(default=0.15)
    cost_shipping = models.FloatField(default=4.0)
    # Disponibilite des offres (desactivable si rupture fournisseur)
    av_30x40 = models.BooleanField("30 x 40 dispo", default=True)
    av_40x50 = models.BooleanField("40 x 50 dispo", default=True)
    av_50x70 = models.BooleanField("50 x 70 dispo", default=True)
    av_60x80 = models.BooleanField("60 x 80 dispo", default=True)
    av_70x100 = models.BooleanField("70 x 100 dispo", default=True)
    av_80x120 = models.BooleanField("80 x 120 dispo", default=True)
    av_c12 = models.BooleanField("12 couleurs dispo", default=True)
    av_c24 = models.BooleanField("24 couleurs dispo", default=True)
    av_c36 = models.BooleanField("36 couleurs dispo", default=True)
    av_brushes = models.BooleanField("Pinceaux dispo", default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Grille tarifaire"
        verbose_name_plural = "Grille tarifaire"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def format_price(self, key):
        return float(getattr(self, "p_" + key, 0.0))

    def color_price(self, n):
        return float(getattr(self, "c_%d" % int(n), 0.0))

    def format_available(self, key):
        return bool(getattr(self, "av_" + key, True))

    def available_formats(self):
        from studio.forms import FORMATS
        keys = [k for k in FORMATS if self.format_available(k)]
        return keys or list(FORMATS)          # jamais vide

    def available_colors(self):
        cols = [n for n in (12, 24, 36) if getattr(self, "av_c%d" % n, True)]
        return cols or [24]

    def __str__(self):
        return "Grille tarifaire"


class Discount(models.Model):
    ISSUED, USED = "issued", "used"
    LOYALTY, PROMO = "loyalty", "promo"
    KIND_CHOICES = [(LOYALTY, "Fidelite (usage unique)"), (PROMO, "Promo (multi-usage)")]
    code = models.CharField(max_length=32, unique=True)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=LOYALTY)
    percent = models.IntegerField("Remise (%)", default=15)
    max_uses = models.IntegerField("Nombre d'utilisations (0 = illimite)", default=1)
    used_count = models.IntegerField(default=0)
    active = models.BooleanField(default=True)
    status = models.CharField(max_length=8, default=ISSUED)   # usage unique (fidelite)
    used_by = models.CharField(max_length=64, null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def remaining(self):
        if self.kind == self.PROMO:
            return "\u221e" if self.max_uses == 0 else max(0, self.max_uses - self.used_count)
        return 0 if self.status == self.USED else 1

    def __str__(self):
        return f"{self.code} ({self.kind}, {self.percent}%)"


class Order(models.Model):
    PENDING, PAID, FULFILLED, SHIPPED, DELIVERED, FAILED = \
        "pending", "paid", "fulfilled", "shipped", "delivered", "failed"
    STATUS_CHOICES = [
        (PENDING, "En attente de paiement"), (PAID, "Payee"),
        (FULFILLED, "Envoyee au fournisseur"), (SHIPPED, "Expediee"),
        (DELIVERED, "Livree"), (FAILED, "Echouee"),
    ]
    uid = models.CharField(max_length=32, unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    lang = models.CharField(max_length=5, default="fr")
    format_label = models.CharField(max_length=40)
    orientation = models.CharField(max_length=12)
    width_cm = models.FloatField()
    height_cm = models.FloatField()
    colors = models.IntegerField()
    difficulty = models.CharField(max_length=12, default="moyen")
    brushes = models.BooleanField(default=False)
    brushes_amount = models.FloatField(default=0.0)
    price = models.FloatField()          # sous-total produit (toile + pinceaux)
    cost = models.FloatField(default=0.0)
    discount_code = models.CharField(max_length=32, null=True, blank=True)
    discount_amount = models.FloatField(default=0.0)
    total = models.FloatField()
    customer_name = models.CharField(max_length=120)
    customer_email = models.EmailField()
    phone_code = models.CharField(max_length=8, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    address1 = models.CharField(max_length=200, blank=True, default="")
    address2 = models.CharField(max_length=200, blank=True, default="")
    postal_code = models.CharField(max_length=20, blank=True, default="")
    city = models.CharField(max_length=120, blank=True, default="")
    country = models.CharField(max_length=80, blank=True, default="")
    supplier_ref = models.CharField(max_length=64, null=True, blank=True)
    # Suivi colis (renseigne d'apres le fournisseur)
    carrier = models.CharField("Transporteur", max_length=60, blank=True, default="")
    tracking_number = models.CharField("N° de suivi", max_length=80, blank=True, default="")
    tracking_url = models.CharField("Lien de suivi", max_length=300, blank=True, default="")
    feedback_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def benefit(self):
        return round(self.total - self.cost, 2)

    def __str__(self):
        return self.uid


class ContactMessage(models.Model):
    name = models.CharField(max_length=120)
    email = models.EmailField()
    subject = models.CharField(max_length=140)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    answered = models.BooleanField(default=False)
    answer = models.TextField(blank=True, default="")
    answered_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.subject} ({self.email})"



class ContactAttachment(models.Model):
    message = models.ForeignKey(ContactMessage, related_name="attachments", on_delete=models.CASCADE)
    file = models.FileField(upload_to="contact/%Y/%m/")
    original_name = models.CharField(max_length=200, blank=True, default="")
    content_type = models.CharField(max_length=80, blank=True, default="")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_name or (self.file.name if self.file else "piece jointe")

class DigitalCanvas(models.Model):
    """Toile numerique d'un joueur (galerie), rattachee a un e-mail verifie si dispo."""
    email = models.EmailField(blank=True, default="")
    uid = models.CharField(max_length=40)
    colors = models.IntegerField(default=24)
    orientation = models.CharField(max_length=12, default="portrait")
    width_cm = models.FloatField(default=40)
    height_cm = models.FloatField(default=50)
    source = models.CharField(max_length=12, default="digital")   # digital | gift | library
    category = models.CharField(max_length=40, blank=True, default="")
    title = models.CharField(max_length=80, blank=True, default="")
    price = models.DecimalField(max_digits=6, decimal_places=2, default=0)  # 0 = gratuit (galerie)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("email", "uid")
        ordering = ["-created_at"]


class EmailCode(models.Model):
    email = models.EmailField()
    code = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)


class PrintPricing(models.Model):
    """Produit 'Tableau fini' (impression) : dimensions, matiere, cadre, verre, fournisseur.
    Tout est editable dans l'admin (capacites boutique)."""
    # Dimensions (prix de base impression, EUR)
    d_30x40 = models.FloatField("30x40", default=29.0)
    d_40x50 = models.FloatField("40x50", default=39.0)
    d_50x70 = models.FloatField("50x70", default=59.0)
    d_60x80 = models.FloatField("60x80", default=75.0)
    d_70x100 = models.FloatField("70x100", default=95.0)
    d_80x120 = models.FloatField("80x120", default=119.0)
    av_30x40 = models.BooleanField("30x40 dispo", default=True)
    av_40x50 = models.BooleanField("40x50 dispo", default=True)
    av_50x70 = models.BooleanField("50x70 dispo", default=True)
    av_60x80 = models.BooleanField("60x80 dispo", default=True)
    av_70x100 = models.BooleanField("70x100 dispo", default=True)
    av_80x120 = models.BooleanField("80x120 dispo", default=True)
    # Matiere (supplement EUR)
    m_toile = models.FloatField("Toile", default=0.0)
    m_alu = models.FloatField("Aluminium", default=20.0)
    m_bois = models.FloatField("Bois", default=15.0)
    av_m_toile = models.BooleanField("Toile dispo", default=True)
    av_m_alu = models.BooleanField("Aluminium dispo", default=True)
    av_m_bois = models.BooleanField("Bois dispo", default=True)
    # Cadre (supplement EUR)
    f_noir = models.FloatField("Cadre noir", default=19.0)
    f_bois = models.FloatField("Cadre bois", default=24.0)
    f_blanc = models.FloatField("Cadre blanc", default=19.0)
    av_f_noir = models.BooleanField("Cadre noir dispo", default=True)
    av_f_bois = models.BooleanField("Cadre bois dispo", default=True)
    av_f_blanc = models.BooleanField("Cadre blanc dispo", default=True)
    # Verre
    av_glass = models.BooleanField("Sous verre dispo", default=True)
    glass_price = models.FloatField("Supplement verre", default=12.0)
    # Fournisseur impression (distinct du kit)
    supplier_email = models.EmailField("E-mail fournisseur impression", blank=True, default="")
    supplier_url = models.CharField("API fournisseur impression", max_length=300, blank=True, default="")

    class Meta:
        verbose_name = "Tarifs Tableau fini (impression)"
        verbose_name_plural = "Tarifs Tableau fini (impression)"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def dims(self):
        keys = [("30x40", self.d_30x40, self.av_30x40), ("40x50", self.d_40x50, self.av_40x50),
                ("50x70", self.d_50x70, self.av_50x70), ("60x80", self.d_60x80, self.av_60x80),
                ("70x100", self.d_70x100, self.av_70x100), ("80x120", self.d_80x120, self.av_80x120)]
        return [{"key": k, "price": p} for (k, p, a) in keys if a]

    def materials(self):
        m = [("toile", "Toile", self.m_toile, self.av_m_toile),
             ("alu", "Aluminium", self.m_alu, self.av_m_alu),
             ("bois", "Bois", self.m_bois, self.av_m_bois)]
        return [{"key": k, "label": l, "price": p} for (k, l, p, a) in m if a]

    def frames(self):
        f = [("sans", "Sans cadre", 0.0, True), ("noir", "Cadre noir", self.f_noir, self.av_f_noir),
             ("bois", "Cadre bois", self.f_bois, self.av_f_bois),
             ("blanc", "Cadre blanc", self.f_blanc, self.av_f_blanc)]
        return [{"key": k, "label": l, "price": p} for (k, l, p, a) in f if a]

    def cfg(self):
        return {"dims": {d["key"]: d["price"] for d in self.dims()},
                "materials": {m["key"]: m["price"] for m in self.materials()},
                "frames": {f["key"]: f["price"] for f in self.frames()},
                "glass": self.glass_price if self.av_glass else None}


class KitFormat(models.Model):
    """Formats du kit (peinture par numeros) gerables depuis /admin-tarifs/ :
    dimensions + prix de base + disponibilite. Ajout/suppression libre."""
    width_cm = models.PositiveIntegerField("Largeur (cm)", default=40)
    height_cm = models.PositiveIntegerField("Hauteur (cm)", default=50)
    price = models.DecimalField("Prix (EUR)", max_digits=7, decimal_places=2, default=34.90)
    available = models.BooleanField("Disponible", default=True)
    sort = models.PositiveIntegerField("Ordre", default=0)

    class Meta:
        ordering = ["sort", "width_cm", "height_cm"]
        unique_together = ("width_cm", "height_cm")

    @property
    def label(self):
        return "%d x %d cm" % (self.width_cm, self.height_cm)

    @property
    def key(self):
        return "%dx%d" % (self.width_cm, self.height_cm)

    def __str__(self):
        return self.label
