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
    supplier = models.ForeignKey("Supplier", verbose_name="Fournisseur", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="orders")
    # Suivi colis (renseigne d'apres le fournisseur)
    carrier = models.CharField("Transporteur", max_length=60, blank=True, default="")
    tracking_number = models.CharField("N° de suivi", max_length=80, blank=True, default="")
    tracking_url = models.CharField("Lien de suivi", max_length=300, blank=True, default="")
    feedback_sent = models.BooleanField(default=False)
    notes = models.TextField("Notes internes", blank=True, default="")
    status_changed_at = models.DateTimeField("Statut depuis", null=True, blank=True)
    ad_ref = models.CharField("Pub d'origine (A/B)", max_length=24, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Commande"
        verbose_name_plural = "Commandes"

    # Statuts qui comptent comme chiffre d'affaires encaisse
    PAID_STATUSES = (PAID, FULFILLED, SHIPPED, DELIVERED)

    @property
    def benefit(self):
        return round(self.total - self.cost, 2)

    def __str__(self):
        return self.uid


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
    created_at = models.DateTimeField(auto_now_add=True)

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


class Supplier(models.Model):
    """Fournisseur plug-and-play : connecte a un checkout, avec grille tarifaire,
    conditions de partenariat et integration (e-mail ou API)."""
    CHECKOUTS = [("kit", "Kit a peindre"), ("print", "Tableau fini"), ("all", "Tous les checkouts")]
    INTEGRATIONS = [("email", "E-mail"), ("api", "API (webhook)")]
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
    created_at = models.DateTimeField(auto_now_add=True)

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
             ("want_order_json", "%s_supplier.json")]
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
