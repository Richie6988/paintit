import os
from django.conf import settings
from django.db import models
from django.utils import timezone


COLOR_COUNTS = (12, 24, 36, 48)


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
    c_48 = models.FloatField("48 couleurs (+)", default=24.0)
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
    av_c48 = models.BooleanField("48 couleurs dispo", default=False)   # a activer dans Tarifs
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
        cols = [n for n in COLOR_COUNTS if getattr(self, "av_c%d" % n, True)]
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

    class Meta:
        verbose_name = "Code remise"
        verbose_name_plural = "Codes remise"

    @property
    def remaining(self):
        if self.kind == self.PROMO:
            return "\u221e" if self.max_uses == 0 else max(0, self.max_uses - self.used_count)
        return 0 if self.status == self.USED else 1

    def __str__(self):
        return f"{self.code} ({self.kind}, {self.percent}%)"


class Order(models.Model):
    PENDING, PAID, FULFILLED, PRODUCTION, SHIPPED, DELIVERED, FAILED = \
        "pending", "paid", "fulfilled", "production", "shipped", "delivered", "failed"
    STATUS_CHOICES = [
        (PENDING, "En attente de paiement"), (PAID, "Payee"),
        (FULFILLED, "Envoyee au fournisseur"), (PRODUCTION, "En production"), (SHIPPED, "Expediee"),
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
    supplier_paid_at = models.DateTimeField("Reglee au fournisseur le", null=True, blank=True)
    shopify_order_id = models.CharField("Commande Shopify (id)", max_length=40, blank=True, default="", db_index=True)
    supplier_payment_ref = models.CharField("Reference du reglement fournisseur", max_length=80, blank=True, default="")
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
    supplier = models.ForeignKey("Supplier", verbose_name="Fournisseur", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="orders")
    # Suivi colis (renseigne d'apres le fournisseur)
    carrier = models.CharField("Transporteur", max_length=60, blank=True, default="")
    tracking_number = models.CharField("N° de suivi", max_length=80, blank=True, default="")
    tracking_url = models.CharField("Lien de suivi", max_length=300, blank=True, default="")
    # Suivi colis (studio/parcel.py) : {"id", "state", "dest_arrived_at", "last_mile": {"carrier","number","url"},
    #  "notified_at", "last_check", "checkpoint", "error"}
    parcel = models.JSONField("Suivi colis", default=dict, blank=True)
    feedback_sent = models.BooleanField(default=False)
    invoice_number = models.CharField("N° de facture", max_length=24, blank=True, default="", db_index=True)
    refunded_amount = models.FloatField("Remboursé (EUR)", default=0.0)
    supplier_payment = models.ForeignKey("SupplierPayment", verbose_name="Règlement fournisseur", null=True, blank=True,
                                         on_delete=models.SET_NULL, related_name="orders")

    def assign_invoice_number(self):
        """Numero de facture chronologique et continu (F2026-00001...), attribue une seule fois au paiement."""
        if self.invoice_number:
            return self.invoice_number
        from django.db import transaction
        with transaction.atomic():
            info = CompanyInfo.objects.select_for_update().get_or_create(pk=1)[0]   # verrou de numerotation
            prefix = "%s%s-" % (info.invoice_prefix or "F", timezone.now().year)
            last = (Order.objects.filter(invoice_number__startswith=prefix)
                    .order_by("-invoice_number").values_list("invoice_number", flat=True).first())
            n = int(last.rsplit("-", 1)[1]) + 1 if last else 1
            self.invoice_number = "%s%05d" % (prefix, n)
            Order.objects.filter(pk=self.pk, invoice_number="").update(invoice_number=self.invoice_number)
        return self.invoice_number
    notes = models.TextField("Notes internes", blank=True, default="")
    status_changed_at = models.DateTimeField("Statut depuis", null=True, blank=True)
    ad_ref = models.CharField("Pub d'origine (A/B)", max_length=24, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Commande"
        verbose_name_plural = "Commandes"

    # Statuts qui comptent comme chiffre d'affaires encaisse
    PAID_STATUSES = (PAID, FULFILLED, PRODUCTION, SHIPPED, DELIVERED)
    AT_SUPPLIER = (FULFILLED, PRODUCTION)          # transmise, pas encore expediee
    FLOW = (PENDING, PAID, FULFILLED, PRODUCTION, SHIPPED, DELIVERED)

    @property
    def benefit(self):
        return round(self.total - self.cost, 2)

    def __str__(self):
        return self.uid


class CompanyInfo(models.Model):
    """Informations legales de l'entreprise (mentions legales, CGV, factures). Singleton editable."""
    legal_name = models.CharField("Raison sociale / nom", max_length=120, default="")
    legal_form = models.CharField("Forme juridique", max_length=60, blank=True, default="",
                                  help_text="ex. SAS, SARL, Entreprise individuelle (micro-entreprise)")
    capital = models.CharField("Capital social", max_length=40, blank=True, default="")
    address = models.TextField("Adresse du siège", blank=True, default="")
    siret = models.CharField("SIRET", max_length=20, blank=True, default="")
    rcs = models.CharField("RCS / RM", max_length=80, blank=True, default="", help_text="ex. RCS Paris 123 456 789")
    vat_number = models.CharField("N° TVA intracommunautaire", max_length=20, blank=True, default="")
    vat_rate = models.FloatField("Taux de TVA (%)", default=0.0,
                                 help_text="0 = franchise en base (« TVA non applicable, art. 293 B du CGI »)")
    director = models.CharField("Directeur de la publication", max_length=120, blank=True, default="")
    email = models.EmailField("E-mail de contact", blank=True, default="contact@paintit.click")
    phone = models.CharField("Téléphone", max_length=30, blank=True, default="")
    host = models.TextField("Hébergeur (nom, adresse, téléphone)", blank=True, default="")
    mediator = models.TextField("Médiateur de la consommation (nom, site)", blank=True, default="")
    invoice_prefix = models.CharField("Préfixe des factures", max_length=10, default="F")
    bank_iban = models.CharField("IBAN de l'entreprise", max_length=40, blank=True, default="")
    bank_bic = models.CharField("BIC de l'entreprise", max_length=20, blank=True, default="")
    production_origin = models.CharField(
        "Lieu de fabrication des kits", max_length=60, blank=True, default="Europe",
        help_text="Zone de fabrication réelle (ex. Europe) : sert à la politique de confidentialité. "
                  "Le site affiche « Fabriqué à la demande par nos ateliers partenaires ».")

    class Meta:
        verbose_name = "Infos légales"
        verbose_name_plural = "Infos légales"

    def __str__(self):
        return self.legal_name or "Infos légales"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def complete(self):
        return bool(self.legal_name and self.address and self.siret and self.host)


class StaffTOTP(models.Model):
    """Double authentification (application TOTP) d'un compte admin."""
    user = models.OneToOneField("auth.User", on_delete=models.CASCADE, related_name="totp")
    secret = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Double authentification"
        verbose_name_plural = "Double authentification"


class OrderEvent(models.Model):
    """Historique d'une commande (journal ERP) : changements de statut (auto) + notes/actions."""
    order = models.ForeignKey(Order, related_name="events", on_delete=models.CASCADE)
    at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=12, default="status")   # status | note | email | action
    status = models.CharField(max_length=10, blank=True, default="")
    text = models.CharField(max_length=300, blank=True, default="")
    user = models.CharField(max_length=150, blank=True, default="")

    class Meta:
        ordering = ["-at"]
        verbose_name = "Evenement commande"
        verbose_name_plural = "Historique commandes"

    @property
    def status_label(self):
        return dict(Order.STATUS_CHOICES).get(self.status, self.status)

    def __str__(self):
        return "%s %s %s" % (self.order_id, self.kind, self.status or self.text)


def _order_pre_save(sender, instance, **kw):
    from django.utils import timezone
    old = None
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    instance._erp_status_changed = old != instance.status
    if instance._erp_status_changed or not instance.status_changed_at:
        instance.status_changed_at = timezone.now()


def _order_post_save(sender, instance, created, **kw):
    if getattr(instance, "_erp_status_changed", False):
        OrderEvent.objects.create(order=instance, kind="status", status=instance.status,
                                  text="Creee" if created else "",
                                  user=getattr(instance, "_erp_user", "") or "")


models.signals.pre_save.connect(_order_pre_save, sender=Order, dispatch_uid="erp_order_pre")
models.signals.post_save.connect(_order_post_save, sender=Order, dispatch_uid="erp_order_post")


class ContactMessage(models.Model):
    name = models.CharField(max_length=120)
    email = models.EmailField()
    subject = models.CharField(max_length=140)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    answered = models.BooleanField(default=False)
    answer = models.TextField(blank=True, default="")
    answered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Message contact"
        verbose_name_plural = "Messages contact"

    def __str__(self):
        return f"{self.subject} ({self.email})"



class MessageReply(models.Model):
    """Reponse envoyee a un message de contact (historique complet de la conversation)."""
    message = models.ForeignKey(ContactMessage, related_name="replies", on_delete=models.CASCADE)
    body = models.TextField()
    attachments = models.JSONField(default=list, blank=True)   # noms des pieces jointes envoyees
    user = models.CharField(max_length=150, blank=True, default="")
    sent = models.BooleanField(default=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at"]
        verbose_name = "Reponse"
        verbose_name_plural = "Reponses"


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
    origin_uid = models.CharField("Toile d'origine", max_length=40, blank=True, default="")   # modele publie depuis une toile client
    in_slider = models.BooleanField("Dans le slider de l'accueil", default=True)
    showcase_slug = models.CharField("Assets du slider", max_length=80, blank=True, default="")   # <slug>_pbn.png / _template.png
    zones = models.IntegerField("Zones à peindre", default=0)   # calcule depuis le SVG du jeu (0 = inconnu)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def difficulty(self):
        return difficulty_level(self.zones, self.colors)

    class Meta:
        unique_together = ("email", "uid")
        ordering = ["-created_at"]
        verbose_name = "Toile / modele"
        verbose_name_plural = "Toiles & modeles (catalogue)"


class EmailCode(models.Model):
    email = models.EmailField()
    code = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Code de verification"
        verbose_name_plural = "Codes de verification"


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


def difficulty_level(zones, colors):
    """Difficulte 1 a 3 etoiles : nombre de zones + poids du nombre de couleurs."""
    score = (zones or 0) + 10 * (colors or 0)
    return 1 if score < 500 else 2 if score < 1200 else 3


def format_key(w, h):
    """Cle de format independante de l'orientation : 50x40 (paysage) -> '40x50' (cle de KitFormat)."""
    a, b = sorted((int(round(float(w or 0))), int(round(float(h or 0)))))
    return "%dx%d" % (a, b)


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
        return format_key(self.width_cm, self.height_cm)

    def __str__(self):
        return self.label


class Supplier(models.Model):
    """Fournisseur plug-and-play : connecte a un checkout, avec grille tarifaire,
    conditions de partenariat et integration (e-mail ou API)."""
    CHECKOUTS = [("kit", "Kit à peindre"), ("print", "Tableau fini"), ("all", "Tous les produits")]
    INTEGRATIONS = [("email", "E-mail"), ("api", "API"), ("shopify", "Shopify"), ("woo", "MangoERP")]
    name = models.CharField("Nom", max_length=120)
    active = models.BooleanField("Actif", default=True)
    checkout = models.CharField("Checkout connecte", max_length=10, choices=CHECKOUTS, default="kit")
    integration = models.CharField("Integration", max_length=10, choices=INTEGRATIONS, default="email")
    email = models.EmailField("E-mail commande", blank=True, default="")
    api_url = models.URLField("URL API / webhook", blank=True, default="")
    api_key = models.CharField("Cle API", max_length=200, blank=True, default="")
    pricing = models.TextField("Grille tarifaire", blank=True, default="")
    terms = models.TextField("Conditions de partenariat", blank=True, default="")
    priority = models.PositiveIntegerField("Priorite (0 = premier)", default=0)
    # Coordonnees
    contact_name = models.CharField("Contact", max_length=120, blank=True, default="")
    contact_email = models.EmailField("E-mail contact", blank=True, default="")
    phone = models.CharField("Telephone", max_length=40, blank=True, default="")
    address = models.TextField("Adresse", blank=True, default="")
    # Banque
    bank_name = models.CharField("Banque", max_length=120, blank=True, default="")
    iban = models.CharField("IBAN", max_length=40, blank=True, default="")
    bic = models.CharField("BIC", max_length=20, blank=True, default="")
    notes = models.TextField("Notes internes", blank=True, default="")
    # Fichiers a envoyer a ce fournisseur (consensus)
    want_source = models.BooleanField("Photo source", default=True)
    want_template_svg = models.BooleanField("Toile numerotee .svg", default=True)
    want_template_tiff = models.BooleanField("Toile numerotee .tiff", default=True)
    want_preview_svg = models.BooleanField("Apercu colorie .svg", default=True)
    want_poster = models.BooleanField("Poster (PNG)", default=True)
    want_order_json = models.BooleanField("Fiche commande JSON (livraison/format/couleurs, sans prix)", default=True)
    want_order_txt = models.BooleanField("Fiche commande texte (lisible : commande, destinataire, n° -> réf. couleur)", default=True)
    want_colors_jpg = models.BooleanField("Tableau des couleurs en image (code usine, n° sur la toile, pastille)", default=True)
    want_template_pdf = models.BooleanField("Toile numerotee PDF vectoriel", default=True)
    # Process
    lead_time_days = models.PositiveIntegerField("Delai production (jours)", default=5)
    incoterms = models.CharField("Incoterms / livraison", max_length=60, blank=True, default="")
    production_notes = models.TextField("Process / consignes de production", blank=True, default="")
    # Automatisation (plug & play)
    auto_dispatch = models.BooleanField("Transmission automatique au paiement", default=True,
        help_text="Sinon la commande attend une validation manuelle (Centre d'actions).")
    webhook_secret = models.CharField("Secret webhook", max_length=64, blank=True, default="",
        help_text="Signe les envois (X-PaintIt-Signature) et authentifie les retours du fournisseur.")
    # Sante de l'integration (derniere transmission)
    last_sync_at = models.DateTimeField("Derniere transmission", null=True, blank=True)
    last_sync_ok = models.BooleanField("Derniere transmission OK", default=True)
    last_sync_error = models.CharField("Derniere erreur", max_length=300, blank=True, default="")
    # Nuancier du fournisseur (import CSV) : [{"code": "1", "hex": "#FFFFFF"}, ...].
    # S'il est rempli, chaque couleur calculee est remplacee par la teinte disponible la plus proche.
    palette = models.JSONField("Nuancier (couleurs disponibles)", default=list, blank=True)
    palette_updated_at = models.DateTimeField("Nuancier importe le", null=True, blank=True)
    # Offre du fournisseur : formats et nombres de couleurs proposes + cout d'achat, options payantes.
    # {"formats": {"40x50": {"on": true, "cost": 12.5}}, "colors": {"24": {"on": true, "cost": 0}},
    #  "options": [{"name": "Set de pinceaux", "cost": 1.2}]}
    offer = models.JSONField("Offre et tarifs", default=dict, blank=True)
    # Boutique Shopify relais (le fournisseur y branche son ERP, ex. MangoERP) :
    # {"domain", "client_id", "client_secret", "static_token", "token", "token_exp", "api_version",
    #  "product_id", "variants": {sku: gid}, "webhooks": [...], "last_check": ...}
    shopify = models.JSONField("Shopify", default=dict, blank=True)
    # Connecteur compatible WooCommerce (l'ERP du fournisseur lie paintit.click comme une boutique Woo) :
    # {"ck": "ck_...", "cs": "cs_...", "created": iso, "last_seen": iso}
    woo = models.JSONField("Connecteur WooCommerce", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def offer_formats():
        """Formats proposables = formats du Kit geres dans l'onglet Tarifs (KitFormat), sinon la liste fixe."""
        fm = [(k.key, k.label) for k in KitFormat.objects.all()]
        if not fm:
            from .forms import FORMATS
            fm = [(k, v[2]) for k, v in FORMATS.items()]
        return fm

    # ---- Grille tarifaire (devis fournisseur) : offer["grid"]
    # {"currency": "USD", "fx": 0.92 (EUR pour 1 unite), "cols": [24, 36, 48],
    #  "zones": [{"key": "z1", "label": "UK", "countries": "GB", "days": "7-12"}],   countries : ISO2, "EU" ou "*"
    #  "products": [{"key": "unframed", "label": "Unframe kit", "kit": true, "extra": {"36": 1, "48": 2}}],
    #  "rows": [{"product": "unframed", "size": "30x40", "on": true, "price": {"24": 5.5}, "ship": {"z1": 6.5}}]}
    GRID_TEMPLATE = {
        "currency": "USD", "fx": 0.92, "cols": [24, 36, 48],
        "zones": [{"key": "fr", "label": "France", "countries": "FR", "days": ""},
                  {"key": "eu", "label": "Union européenne", "countries": "EU", "days": ""},
                  {"key": "uk", "label": "UK", "countries": "GB", "days": "7-12"},
                  {"key": "us", "label": "USA", "countries": "US", "days": "7-12"},
                  {"key": "nz", "label": "NZ", "countries": "NZ", "days": "7-12"}],
        "products": [{"key": "unframed", "label": "Unframe kit", "kit": True, "extra": {"36": 1, "48": 2}},
                     {"key": "frame", "label": "DIY frame kit", "kit": False, "extra": {"36": 1, "48": 2}}],
        "rows": [{"product": "unframed", "size": sz, "on": True, "price": {"24": a, "36": b, "48": c},
                  "ship": {"uk": u, "us": us, "nz": nz}} for sz, a, b, c, u, us, nz in (
            ("30x40", 5.5, 7.5, 8.5, 6.5, 7.8, 7.8), ("40x40", 6.5, 8.5, 9.5, 8.0, 9.5, 9.5),
            ("40x50", 7.5, 9.5, 10.5, 8.5, 10.5, 10.5), ("40x60", 8.0, 10.0, 11.0, 10.0, 11.0, 11.0),
            ("50x50", 8.5, 10.5, 11.5, 11.0, 13.5, 12.5), ("50x60", 9.0, 11.0, 12.0, 12.0, 15.5, 14.0),
            ("50x70", 9.5, 11.5, 12.5, 13.0, 17.0, 16.0), ("50x75", 10.5, 12.5, 13.5, 15.0, 19.0, 18.0))] +
                [{"product": "frame", "size": sz, "on": True, "price": {"24": a, "36": b, "48": c},
                  "ship": {"uk": u, "us": us, "nz": nz}} for sz, a, b, c, u, us, nz in (
            ("30x40", 7.2, 9.2, 10.2, 10.8, 10.2, 7.8), ("40x40", 8.2, 10.2, 11.2, 12.0, 11.5, 9.5),
            ("40x50", 8.8, 10.8, 11.8, 18.0, 15.5, 10.5))],
    }

    def grid(self):
        return (self.offer or {}).get("grid") or {}

    def grid_fx(self):
        g = self.grid()
        return 1.0 if (g.get("currency") or "EUR") == "EUR" else float(g.get("fx") or 0) or 1.0

    def grid_kit_product(self):
        prods = self.grid().get("products") or []
        return next((p for p in prods if p.get("kit")), prods[0] if prods else None)

    def grid_row(self, fmt_key, product=None):
        p = product or (self.grid_kit_product() or {}).get("key")
        return next((r for r in self.grid().get("rows") or []
                     if r.get("product") == p and r.get("on") and format_key(*r["size"].split("x")) == fmt_key), None)

    def grid_zone(self, iso2):
        """Zone de livraison du devis pour un pays (ISO2) : pays explicite, sinon « EU », sinon « * »."""
        zones = self.grid().get("zones") or []
        iso2 = (iso2 or "FR").upper()
        lists = [(z, [c.strip().upper() for c in (z.get("countries") or "").replace(";", ",").split(",") if c.strip()])
                 for z in zones]
        from .supplier import EU
        for test in (lambda cs: iso2 in cs, lambda cs: "EU" in cs and (iso2 in EU or iso2 == "FR"), lambda cs: "*" in cs):
            for z, cs in lists:
                if test(cs):
                    return z
        return None

    def _num(self, d, k):
        v = (d or {}).get(str(k))
        return None if v in (None, "") else float(v)

    def grid_goods(self, fmt_key, colors, product=None):
        """Prix unitaire du kit (devise du devis) ou None."""
        r = self.grid_row(fmt_key, product)
        return self._num(r.get("price"), int(colors)) if r else None

    def grid_shipping(self, fmt_key, colors, iso2="FR", product=None):
        """Livraison (devise du devis) : colonne de la zone + supplement couleurs du produit. None si non chiffree."""
        r = self.grid_row(fmt_key, product)
        z = self.grid_zone(iso2)
        if not r or not z:
            return None
        base = self._num(r.get("ship"), z["key"])
        if base is None:
            return None
        prod = next((p for p in self.grid().get("products") or [] if p.get("key") == r.get("product")), {})
        return base + (self._num(prod.get("extra"), int(colors)) or 0.0)

    def offer_get(self, group, key):
        if self.grid() and group in ("formats", "colors"):
            fx = self.grid_fx()
            cols = [int(c) for c in self.grid().get("cols") or []]
            if group == "formats":
                p = self.grid_goods(str(key), cols[0]) if cols else None
                return {"on": p is not None, "cost": round(p * fx, 2) if p is not None else None}
            if int(key) not in cols:
                return {"on": False, "cost": None}
            diffs = [(self._num(r.get("price"), key) or 0) - (self._num(r.get("price"), cols[0]) or 0)
                     for r in self.grid().get("rows") or [] if r.get("on")
                     and r.get("product") == (self.grid_kit_product() or {}).get("key")]
            return {"on": True, "cost": round(sum(diffs) / len(diffs) * fx, 2) if diffs else 0.0}
        return ((self.offer or {}).get(group) or {}).get(str(key)) or {}

    def offer_cost(self, group, key):
        d = self.offer_get(group, key)
        return d.get("cost") if d.get("on") else None

    def goods_cost(self, fmt_key, colors, brushes=False):
        """Prix d'achat produit (EUR, hors livraison/taxes) ou None."""
        if self.grid():
            p = self.grid_goods(fmt_key, colors)
            if p is None:
                return None
            g = p * self.grid_fx()
        else:
            f = self.offer_cost("formats", fmt_key)
            if f is None:
                return None
            g = f + (self.offer_cost("colors", colors) or 0.0)
        return round(g + ((self.offer_cost("extras", "brushes") or 0.0) if brushes else 0.0), 2)

    def taxes(self):
        t = (self.offer or {}).get("taxes") or {}
        return {"vat_rate": float(t.get("vat_rate") or 0), "vat_recoverable": bool(t.get("vat_recoverable", True)),
                "duties_rate": float(t.get("duties_rate") or 0), "customs_fee": float(t.get("customs_fee") or 0)}

    def order_cost(self, fmt_key, colors, brushes=False, country="FR"):
        """Cout de revient d'une commande chez ce fournisseur (EUR) : produit + droits + livraison + frais
        (+ TVA fournisseur si non recuperable). None si le format, les couleurs ou la livraison ne sont pas tarifes."""
        goods = self.goods_cost(fmt_key, colors, brushes)
        if goods is None:
            return None
        sh = (self.offer or {}).get("shipping") or {}
        if self.grid():
            ship = self.grid_shipping(fmt_key, colors, country)
            if ship is None:
                return None
            ship *= self.grid_fx()
        else:
            from .supplier import EU
            c = (country or "FR").upper()
            ship = float(sh.get("fr" if c in ("FR", "fr") else "eu" if c in EU or c == "eu" else "world") or 0)
        t = self.taxes()
        cost = goods * (1 + t["duties_rate"] / 100.0) + t["customs_fee"] + ship + float(sh.get("handling") or 0)
        if not t["vat_recoverable"]:
            cost *= 1 + t["vat_rate"] / 100.0
        return round(cost, 2)

    def save(self, *args, **kwargs):
        if not self.webhook_secret:
            import secrets
            self.webhook_secret = secrets.token_urlsafe(24)
        super().save(*args, **kwargs)

    def wanted_files(self, uid):
        """Noms de fichiers coches (fixes). La photo source (nom variable) est ajoutee a part."""
        m = [("want_template_pdf", "%s_template.pdf"),
             ("want_template_svg", "%s_template.svg"), ("want_template_tiff", "%s_template.tiff"),
             ("want_preview_svg", "%s_preview.svg"),
             ("want_poster", "%s_poster.png"),
             ("want_order_json", "%s_supplier.json"), ("want_order_txt", "%s_supplier.txt"),
             ("want_colors_jpg", "%s_supplier_colors.jpg")]
        return [(pat % uid) for flag, pat in m if getattr(self, flag, False)]

    class Meta:
        ordering = ["priority", "name"]
        verbose_name = "Fournisseur"
        verbose_name_plural = "Fournisseurs"

    def __str__(self):
        return self.name

    @classmethod
    def for_checkout(cls, checkout):
        """Fournisseur actif connecte a ce checkout (ou 'all'), par priorite."""
        return (cls.objects.filter(active=True)
                .filter(models.Q(checkout=checkout) | models.Q(checkout="all"))
                .order_by("priority", "id").first())


class MarketingAd(models.Model):
    """Pub produite par le Marketing Corner (galerie A/B) : textes, fichier, lien traque + compteurs.
    Une 'variante' = une generation (meme image, textes differents) ; les pubs d'un meme
    `image_uid` et d'un meme `kind` sont comparees entre variantes."""
    FORMATS = [("web", "Page web animee"), ("gif", "GIF")]
    token = models.CharField(max_length=24, unique=True)
    image_uid = models.CharField("Image", max_length=40, db_index=True)
    campaign = models.CharField("Campagne", max_length=80, blank=True, default="")
    variant = models.CharField("Variante", max_length=40, default="A")
    kind = models.CharField("Gabarit", max_length=20)
    label = models.CharField(max_length=80, blank=True, default="")
    fmt = models.CharField(max_length=4, choices=FORMATS, default="web")
    file_name = models.CharField(max_length=120)
    target_url = models.CharField("Lien cible", max_length=300, default="https://paintit.click/create/")
    texts = models.JSONField(default=dict, blank=True)
    views = models.PositiveIntegerField("Vues (visiteurs uniques)", default=0)
    clicks = models.PositiveIntegerField("Clics (total)", default=0)
    unique_clicks = models.PositiveIntegerField("Clics (visiteurs uniques)", default=0)
    test_clicks = models.PositiveIntegerField("Clics internes (staff)", default=0)
    last_click_at = models.DateTimeField("Dernier clic", null=True, blank=True)
    active = models.BooleanField("Active", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Pub marketing"
        verbose_name_plural = "Pubs marketing (A/B)"

    @property
    def ctr(self):
        """Taux de clic = visiteurs ayant clique / visiteurs ayant vu la pub."""
        return round(min(self.unique_clicks, self.views) / self.views * 100, 1) if self.views else None

    def __str__(self):
        return "%s %s/%s" % (self.image_uid, self.variant, self.kind)


def _supplier_file_path(instance, filename):
    import secrets
    base, ext = os.path.splitext(os.path.basename(filename))
    # media/private/ : bloque par nginx ; prefixe aleatoire = URL non devinable
    return "private/suppliers/%s/%s_%s%s" % (instance.supplier_id, secrets.token_hex(6), base[:60], ext[:10])


class SupplierFile(models.Model):
    """Piece jointe d'un fournisseur (contrat, catalogue, grille PDF...). Servie uniquement au staff."""
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to=_supplier_file_path, max_length=255)
    name = models.CharField(max_length=160)
    size = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
        verbose_name = "Piece jointe fournisseur"
        verbose_name_plural = "Pieces jointes fournisseur"

    def __str__(self):
        return self.name


class SupplierPayment(models.Model):
    """Reglement d'un lot de commandes a un fournisseur (virement) : prepare -> confirme."""
    DRAFT, SENT = "draft", "sent"
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="payments")
    status = models.CharField(max_length=8, choices=[(DRAFT, "À virer"), (SENT, "Viré")], default=DRAFT)
    reference = models.CharField("Référence du virement", max_length=40)
    amount_eur = models.FloatField("Montant (EUR)", default=0.0)
    currency = models.CharField("Devise du fournisseur", max_length=3, default="EUR")
    amount_native = models.FloatField("Montant (devise fournisseur)", default=0.0)
    paid_at = models.DateField("Date du virement", null=True, blank=True)
    bank_ref = models.CharField("Référence bancaire", max_length=80, blank=True, default="")
    created_by = models.CharField(max_length=80, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Règlement fournisseur"
        verbose_name_plural = "Règlements fournisseurs"

    def __str__(self):
        return self.reference


class LedgerEntry(models.Model):
    """Grand livre simplifie : chaque mouvement d'argent, rapproche de Stripe et des commandes.
    Montants en EUR, signes du point de vue de l'entreprise (vente +, remboursement -, frais -, achat -)."""
    KINDS = [("sale", "Vente"), ("refund", "Remboursement"), ("fee", "Frais de paiement"), ("supplier", "Règlement fournisseur"),
             ("payout", "Virement Stripe → banque")]
    SOURCES = [("kit", "Kit"), ("digital", "Toile numérique"), ("gallery", "Modèle galerie"), ("supplier", "Fournisseur"),
               ("stripe", "Stripe")]
    date = models.DateTimeField(db_index=True)
    kind = models.CharField(max_length=10, choices=KINDS)
    source = models.CharField(max_length=10, choices=SOURCES, default="kit")
    order = models.ForeignKey(Order, null=True, blank=True, on_delete=models.SET_NULL, related_name="ledger")
    supplier_payment = models.ForeignKey(SupplierPayment, null=True, blank=True, on_delete=models.CASCADE, related_name="ledger")
    uid = models.CharField("Référence produit", max_length=40, blank=True, default="")
    label = models.CharField(max_length=200, blank=True, default="")
    reference = models.CharField("Pièce", max_length=40, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    country = models.CharField(max_length=2, blank=True, default="")
    amount = models.FloatField("Montant TTC (EUR)")
    vat = models.FloatField("dont TVA (EUR)", default=0.0)
    fee = models.FloatField("Frais Stripe (EUR)", default=0.0)
    stripe_id = models.CharField(max_length=80, blank=True, default="", db_index=True)   # pi_/re_/po_ : dedoublonnage
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]
        verbose_name = "Écriture"
        verbose_name_plural = "Grand livre"
        constraints = [models.UniqueConstraint(fields=["kind", "stripe_id"], condition=~models.Q(stripe_id=""),
                                               name="ledger_unique_stripe")]

    @property
    def net(self):
        return round(self.amount - self.fee, 2)


class ConnectorLog(models.Model):
    """Journal des appels de l'ERP du fournisseur sur le connecteur WooCommerce (diagnostic du format)."""
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.CASCADE, related_name="connector_logs")
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=300)
    query = models.CharField(max_length=500, blank=True, default="")
    status = models.PositiveSmallIntegerField(default=200)
    ip = models.CharField(max_length=45, blank=True, default="")
    agent = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField(blank=True, default="")
    note = models.CharField(max_length=200, blank=True, default="")
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at"]


class Community(models.Model):
    """Communaute ou PaintIt peut participer (sous compte de marque assume) : regles d'autopromotion, audience."""
    PLATFORMS = [("reddit", "Reddit"), ("facebook", "Groupe Facebook"), ("forum", "Forum"), ("discord", "Discord"),
                 ("social", "Réseau social (hashtag)"), ("launch", "Plateforme de lancement"), ("other", "Autre")]
    REGIONS = [("global", "Monde (anglais)"), ("eu", "Europe"), ("fr", "France"), ("de", "Allemagne/Autriche"),
               ("es", "Espagne"), ("it", "Italie"), ("us", "États-Unis"), ("au", "Australie")]
    PROMO = [("unknown", "À vérifier"), ("forbidden", "Interdite"), ("limited", "Encadrée (jour/fil dédié)"),
             ("allowed", "Autorisée (avec transparence)")]
    platform = models.CharField(max_length=10, choices=PLATFORMS, default="reddit")
    name = models.CharField("Nom", max_length=120)
    url = models.URLField(max_length=300)
    region = models.CharField(max_length=8, choices=REGIONS, default="global")
    lang = models.CharField("Langue", max_length=5, default="en")
    members = models.PositiveIntegerField("Membres", null=True, blank=True)
    members_checked_at = models.DateTimeField(null=True, blank=True)
    promo = models.CharField("Autopromotion", max_length=10, choices=PROMO, default="unknown")
    rules = models.TextField("Règles (résumé / texte)", blank=True, default="")
    angle = models.TextField("Angle conseillé", blank=True, default="")
    notes = models.TextField(blank=True, default="")
    priority = models.PositiveSmallIntegerField("Priorité (1 = haute)", default=2)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "region", "-members", "name"]
        verbose_name = "Communauté"
        verbose_name_plural = "Communautés"

    def __str__(self):
        return self.name

    @property
    def subreddit(self):
        import re
        m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", self.url or "")
        return m.group(1) if m else ""


class Outreach(models.Model):
    """Une publication (ou reponse) dans une communaute, avec son lien traque /go/<code>/ et ses statistiques."""
    community = models.ForeignKey(Community, null=True, blank=True, on_delete=models.SET_NULL, related_name="posts")
    template = models.ForeignKey("PostTemplate", null=True, blank=True, on_delete=models.SET_NULL, related_name="posts")
    campaign = models.ForeignKey("Campaign", null=True, blank=True, on_delete=models.SET_NULL, related_name="posts")
    code = models.CharField(max_length=16, unique=True)
    title = models.CharField("Titre", max_length=300, blank=True, default="")
    body = models.TextField("Texte", blank=True, default="")
    target_url = models.CharField("Lien cible", max_length=300, default="https://paintit.click/create/")
    posted_url = models.URLField("URL de la publication", max_length=400, blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    clicks = models.PositiveIntegerField(default=0)
    unique_clicks = models.PositiveIntegerField(default=0)
    last_click_at = models.DateTimeField(null=True, blank=True)
    score = models.IntegerField(null=True, blank=True)
    comments = models.PositiveIntegerField(null=True, blank=True)
    comments_seen = models.PositiveIntegerField(default=0)
    last_checked = models.DateTimeField(null=True, blank=True)
    removed = models.BooleanField("Retirée par la modération", default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Publication communauté"
        verbose_name_plural = "Publications communautés"

    @property
    def new_replies(self):
        return max(0, (self.comments or 0) - (self.comments_seen or 0))


class PostTemplate(models.Model):
    """Modele de publication reutilisable (bibliotheque) : accroche, texte ({link} = lien traque), brief media."""
    CATEGORIES = [("story", "Histoire / émotion"), ("beforeafter", "Avant / après"), ("gift", "Idée cadeau"),
                  ("tech", "Technique / algorithme"), ("engage", "Question / engagement"), ("challenge", "Défi / communauté"),
                  ("tips", "Conseils de peinture"), ("launch", "Lancement / produit"), ("season", "Saisonnier"),
                  ("family", "Enfants / seniors"), ("b2b", "Partenariats / B2B"), ("ugc", "Clients (avec accord)")]
    MEDIA = [("photo", "Photo"), ("beforeafter", "Photo avant/après"), ("carousel", "Carrousel"),
             ("video", "Vidéo courte"), ("gif", "GIF / timelapse"), ("text", "Texte seul")]
    name = models.CharField("Nom interne", max_length=120)
    category = models.CharField(max_length=12, choices=CATEGORIES, default="story")
    lang = models.CharField("Langue", max_length=5, default="en")
    platforms = models.CharField("Canaux adaptés", max_length=120, default="reddit,facebook",
                                 help_text="reddit, facebook, social, launch, forum, discord (séparés par des virgules)")
    hook = models.CharField("Accroche / titre", max_length=300)
    body = models.TextField("Texte", blank=True, default="", help_text="{link} est remplacé par le lien traqué.")
    media_kind = models.CharField("Média", max_length=12, choices=MEDIA, default="photo")
    media_brief = models.TextField("Brief média (quoi montrer)", blank=True, default="")
    media = models.FileField("Fichier média", upload_to="community/", blank=True, null=True)
    # Visuel genere par le Marketing Corner (MarketingAd image_uid + variant) : pret d'office, modifiable
    ad_uid = models.CharField("Visuel : image", max_length=40, blank=True, default="")
    ad_variant = models.CharField("Visuel : variante", max_length=40, blank=True, default="")
    target_path = models.CharField("Page cible", max_length=120, default="/create/")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "lang", "name"]
        verbose_name = "Modèle de publication"
        verbose_name_plural = "Bibliothèque de publications"

    def __str__(self):
        return self.name

    def visual_kind(self):
        """Format du visuel adapte au media et aux canaux : story (video), carre (Reddit seul), portrait sinon."""
        if self.media_kind == "video":
            return "gif_story"
        pl = self.platform_list()
        return "gif_square" if pl and all(p in ("reddit", "forum", "discord") for p in pl) else "gif_portrait"

    def visual(self):
        """Visuel affiche dans la bibliotheque (MarketingAd) ou None."""
        if not self.ad_uid:
            return None
        ads = list(MarketingAd.objects.filter(image_uid=self.ad_uid, variant=self.ad_variant))
        want = self.visual_kind()
        return next((a for a in ads if a.kind == want), None) or next((a for a in ads if a.fmt == "gif"), None) \
            or (ads[0] if ads else None)

    def platform_list(self):
        return [p.strip() for p in (self.platforms or "").split(",") if p.strip()]


class Review(models.Model):
    """Avis client VERIFIE (lie a une commande livree). Publie apres moderation : on ne masque que les contenus
    abusifs ou hors sujet, jamais un avis parce qu'il est negatif (regles Google + droit de la consommation)."""
    order = models.OneToOneField("Order", on_delete=models.CASCADE, related_name="review")
    rating = models.PositiveSmallIntegerField("Note (1-5)")
    text = models.TextField("Avis", blank=True, default="")
    author = models.CharField("Nom affiché", max_length=60)
    lang = models.CharField(max_length=5, default="fr")
    published = models.BooleanField("Publié", default=True)
    hidden_reason = models.CharField("Motif de masquage", max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Avis client"
        verbose_name_plural = "Avis clients"

    def __str__(self):
        return "%s · %d/5" % (self.author, self.rating)


class Campaign(models.Model):
    """Campagne de croissance : ciblage + contenus + diffusion + retombees (publications, pubs A/B, ventes)."""
    STATUS = [("draft", "Brouillon"), ("active", "En cours"), ("done", "Terminée")]
    name = models.CharField("Nom", max_length=80, unique=True)
    goal = models.TextField("Objectif / message clé", blank=True, default="")
    status = models.CharField(max_length=8, choices=STATUS, default="draft")
    start = models.DateField("Début", null=True, blank=True)
    end = models.DateField("Fin", null=True, blank=True)
    budget = models.FloatField("Budget (€)", default=0)
    # Ciblage (listes separees par des virgules ; vide = tout)
    regions = models.CharField(max_length=120, blank=True, default="")
    langs = models.CharField(max_length=60, blank=True, default="")
    platforms = models.CharField(max_length=120, blank=True, default="")
    categories = models.CharField("Thèmes de contenu", max_length=200, blank=True, default="")
    min_priority = models.PositiveSmallIntegerField("Priorité max. des communautés", default=3)
    exclude_forbidden = models.BooleanField("Exclure les communautés où l'autopromo est interdite", default=True)
    discount = models.ForeignKey("Discount", null=True, blank=True, on_delete=models.SET_NULL, related_name="campaigns")
    append_code = models.BooleanField("Ajouter le code promo aux textes", default=False)
    target_path = models.CharField("Page cible", max_length=120, default="/create/")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Campagne"
        verbose_name_plural = "Campagnes"

    def __str__(self):
        return self.name

    @staticmethod
    def _l(v):
        return [x.strip() for x in (v or "").split(",") if x.strip()]

    def region_list(self):
        return self._l(self.regions)

    def lang_list(self):
        return self._l(self.langs)

    def platform_list(self):
        return self._l(self.platforms)

    def category_list(self):
        return self._l(self.categories)

    def targeting_labels(self):
        """Resume lisible du ciblage (cartes de campagne)."""
        reg = dict(Community.REGIONS); pf = dict(Community.PLATFORMS)
        zones = ", ".join(reg.get(x, x) for x in self.region_list()) or "Toutes zones"
        chans = ", ".join(pf.get(x, x) for x in self.platform_list()) or "Tous canaux"
        langs = " · ".join(x.upper() for x in self.lang_list())
        return {"zones": zones, "channels": chans, "langs": langs}


class SocialVideo(models.Model):
    """Video « satisfying » (TikTok / Reels / Shorts) generee a partir d'une toile du catalogue (jamais d'une photo client)."""
    MODES = [("random", "Aléatoire (accélère à la fin)"), ("colors", "Couleur après couleur")]
    STATUS = [("queued", "En attente"), ("running", "En cours"), ("done", "Prête"), ("error", "Erreur")]
    canvas_uid = models.CharField("Toile", max_length=40)
    title = models.CharField(max_length=120, blank=True, default="")
    mode = models.CharField(max_length=8, choices=MODES, default="random")
    duration = models.FloatField("Durée (s)", default=65)
    hook = models.CharField("Accroche", max_length=120, blank=True, default="")
    end_title = models.CharField("Titre de fin", max_length=120, blank=True, default="")
    end_sub = models.CharField("Sous-titre de fin", max_length=120, blank=True, default="")
    music = models.BooleanField("Musique", default=True)
    music_style = models.CharField("Style de musique", max_length=10, default="ambient")
    before_after = models.BooleanField("Avant / après (photo d'origine au début)", default=True)
    seed = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=8, choices=STATUS, default="queued")
    progress = models.FloatField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Vidéo TikTok"
        verbose_name_plural = "Vidéos TikTok"

    def path(self, ext="mp4"):
        return os.path.join(settings.MEDIA_ROOT, "social", "video-%d.%s" % (self.pk, ext))

    @property
    def file_name(self):
        return "paintit-%s-%d.mp4" % (self.mode, self.pk)


class SiteBanner(models.Model):
    """Bandeau en haut du site (au-dessus du menu). Sans bandeau actif : message standard traduit.
    Priorite si plusieurs sont en ligne : maintenance > promo > info."""
    KINDS = [("promo", "Promo"), ("maintenance", "Maintenance"), ("info", "Information")]
    PRIORITY = {"maintenance": 0, "promo": 1, "info": 2}
    name = models.CharField("Nom interne", max_length=80)
    kind = models.CharField("Type", max_length=12, choices=KINDS, default="promo")
    active = models.BooleanField("Activé", default=True)
    starts_at = models.DateTimeField("Début", null=True, blank=True)
    ends_at = models.DateTimeField("Fin (retour au bandeau standard)", null=True, blank=True)
    countdown_to = models.DateTimeField("Compte à rebours jusqu'à", null=True, blank=True)
    text_fr = models.CharField("Texte (FR)", max_length=160)
    text_en = models.CharField("Texte (EN, sert aussi DE/ES/IT)", max_length=160, blank=True, default="")
    code = models.CharField("Code promo affiché", max_length=40, blank=True, default="")
    link_url = models.CharField("Lien", max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Bandeau du site"
        verbose_name_plural = "Bandeaux du site"

    def is_live(self, now=None):
        from django.utils import timezone
        now = now or timezone.now()
        return self.active and (not self.starts_at or self.starts_at <= now) and (not self.ends_at or now < self.ends_at)

    def text(self, lang):
        return self.text_fr if (lang or "fr")[:2] == "fr" or not self.text_en else self.text_en

    @classmethod
    def current(cls):
        """Bandeau en ligne (cache 60 s, vide a chaque enregistrement)."""
        from django.core.cache import cache
        pk = cache.get("site_banner")
        if pk is None:
            live = [b for b in cls.objects.filter(active=True) if b.is_live()]
            live.sort(key=lambda b: (cls.PRIORITY.get(b.kind, 9), -b.pk))
            pk = live[0].pk if live else 0
            ttl = 60
            cache.set("site_banner", pk, ttl)
        return cls.objects.filter(pk=pk).first() if pk else None

    def save(self, *a, **kw):
        from django.core.cache import cache
        super().save(*a, **kw)
        cache.delete("site_banner")

    def delete(self, *a, **kw):
        from django.core.cache import cache
        r = super().delete(*a, **kw)
        cache.delete("site_banner")
        return r
