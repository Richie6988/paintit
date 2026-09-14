from django.contrib import admin
from django.core.mail import EmailMessage
from django.conf import settings
from django.utils import timezone
from django.utils.html import format_html

from .models import Discount, Order, ContactMessage, ContactAttachment, Pricing, DigitalCanvas, EmailCode, PrintPricing
from . import emails

admin.site.site_header = "PaintIt Admin"
admin.site.site_title = "PaintIt Admin"
admin.site.index_title = "Gestion PaintIt"


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("uid", "status_badge", "product_col", "total", "benefit_col",
                    "tracking_col", "customer_email", "created_at")
    list_filter = ("status", "lang", "carrier", "orientation", "colors", "brushes")
    search_fields = ("uid", "customer_name", "customer_email", "discount_code",
                     "supplier_ref", "tracking_number")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 25
    readonly_fields = ("uid", "created_at", "cost", "benefit_col", "supplier_ref", "feedback_sent")
    actions = ("mark_shipped", "mark_delivered", "send_feedback")
    fieldsets = (
        ("Commande", {"fields": (("uid", "status"), ("created_at", "lang"), "supplier_ref")}),
        ("Suivi & expedition", {"fields": ("carrier", ("tracking_number", "tracking_url"),
                                           "feedback_sent")}),
        ("Produit", {"fields": (("format_label", "orientation"),
                                ("width_cm", "height_cm"), ("colors", "difficulty"),
                                ("brushes", "brushes_amount"))}),
        ("Paiement", {"fields": (("price", "discount_code", "discount_amount"),
                                 ("total", "cost", "benefit_col"))}),
        ("Client & livraison", {"fields": ("customer_name", "customer_email",
                                           ("phone_code", "phone"),
                                           "address1", "address2",
                                           ("postal_code", "city", "country"))}),
    )

    @admin.display(description="Statut", ordering="status")
    def status_badge(self, obj):
        colors = {"fulfilled": "#1f56cf", "paid": "#1f56cf", "shipped": "#5a3fb8",
                  "delivered": "#237a3e", "pending": "#a86a00", "failed": "#b23"}
        bg = {"fulfilled": "#e9f0ff", "paid": "#e9f0ff", "shipped": "#efeaff",
              "delivered": "#e7f6ec", "pending": "#fff4e0", "failed": "#fde8e8"}
        return format_html('<span style="padding:2px 8px;border-radius:999px;font-weight:600;'
                           'font-size:.8em;color:{};background:{}">{}</span>',
                           colors.get(obj.status, "#333"), bg.get(obj.status, "#eee"),
                           obj.get_status_display())

    @admin.display(description="Produit")
    def product_col(self, obj):
        extra = " · pinceaux" if obj.brushes else ""
        return f"{obj.format_label} · {obj.colors}c{extra}"

    @admin.display(description="Marge")
    def benefit_col(self, obj):
        return f"{obj.benefit} \u20ac"

    @admin.display(description="Suivi colis")
    def tracking_col(self, obj):
        if not obj.tracking_number:
            return "\u2014"
        label = f"{obj.carrier} {obj.tracking_number}".strip()
        if obj.tracking_url:
            return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>',
                               obj.tracking_url, label)
        return label

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # E-mail d'avis automatique quand la commande passe a "Livree".
        if obj.status == Order.DELIVERED and not obj.feedback_sent:
            emails.send_feedback_request(obj)
            Order.objects.filter(pk=obj.pk).update(feedback_sent=True)

    @admin.action(description="Marquer comme expediee")
    def mark_shipped(self, request, queryset):
        n = queryset.update(status=Order.SHIPPED)
        self.message_user(request, f"{n} commande(s) marquee(s) expediee(s).")

    @admin.action(description="Marquer comme livree + demander un avis")
    def mark_delivered(self, request, queryset):
        sent = 0
        for o in queryset:
            o.status = Order.DELIVERED
            o.save(update_fields=["status"])
            if not o.feedback_sent:
                emails.send_feedback_request(o)
                Order.objects.filter(pk=o.pk).update(feedback_sent=True)
                sent += 1
        self.message_user(request, f"{queryset.count()} livree(s), {sent} e-mail(s) d'avis envoye(s).")

    @admin.action(description="Envoyer la demande d'avis (e-mail)")
    def send_feedback(self, request, queryset):
        sent = 0
        for o in queryset:
            emails.send_feedback_request(o)
            Order.objects.filter(pk=o.pk).update(feedback_sent=True)
            sent += 1
        self.message_user(request, f"{sent} e-mail(s) d'avis envoye(s).")


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ("code", "kind", "percent", "max_uses", "used_count", "remaining_display",
                    "active", "status", "created_at")
    list_filter = ("kind", "active", "status")
    search_fields = ("code", "used_by")
    ordering = ("-created_at",)
    readonly_fields = ("used_count", "used_by", "used_at", "created_at")
    fieldsets = (
        ("Code", {"fields": (("code", "kind"), ("percent", "max_uses", "active"))}),
        ("Suivi", {"fields": (("used_count", "status"), ("used_by", "used_at"), "created_at")}),
    )

    @admin.display(description="Restant")
    def remaining_display(self, obj):
        return obj.remaining


class ContactAttachmentInline(admin.TabularInline):
    model = ContactAttachment
    extra = 0
    can_delete = False
    readonly_fields = ("apercu", "original_name", "content_type", "uploaded_at")
    fields = ("apercu", "original_name", "content_type", "uploaded_at")

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Fichier")
    def apercu(self, obj):
        if not obj.file:
            return "-"
        url = obj.file.url
        low = (obj.original_name or obj.file.name).lower()
        if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return format_html('<a href="{}" target="_blank"><img src="{}" '
                               'style="max-height:90px;border-radius:8px;border:1px solid #e2e8f2"></a>', url, url)
        return format_html('<a class="button" href="{}" target="_blank">Ouvrir / telecharger</a>', url)


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    inlines = [ContactAttachmentInline]
    list_display = ("subject", "name", "email", "answered", "created_at", "answered_at")
    list_filter = ("answered",)
    search_fields = ("name", "email", "subject", "message")
    ordering = ("-created_at",)
    readonly_fields = ("name", "email", "subject", "message", "created_at", "answered_at")
    fields = ("name", "email", "subject", "message", "created_at", "answer", "answered", "answered_at")

    def save_model(self, request, obj, form, change):
        # Repondre depuis l'admin : si une reponse est saisie, on l'envoie au client.
        if obj.answer.strip() and not obj.answered:
            EmailMessage(
                subject=f"Re: {obj.subject}",
                body=obj.answer,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[obj.email],
            ).send(fail_silently=True)
            obj.answered = True
            obj.answered_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(Pricing)
class PricingAdmin(admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        from django.urls import reverse
        obj = Pricing.get()
        return redirect(reverse("admin:studio_pricing_change", args=[obj.pk]))

    fieldsets = (
        ("Prix par format (EUR)", {"fields": (("p_30x40", "p_40x50"),
                                              ("p_50x70", "p_60x80"),
                                              ("p_70x100", "p_80x120"))}),
        ("Supplement selon le nombre de couleurs (EUR)", {"fields": (("c_12", "c_24", "c_36"),)}),
        ("Options", {"fields": ("brushes_price",)}),
        ("Remise, parrainage & livraison", {"fields": (("discount_rate", "free_shipping"),
                                           ("referral_rate", "referral_physical_rate"),
                                           ("delivery_days_min", "delivery_days_max"))}),
        ("Disponibilite des offres", {"fields": (
            ("av_30x40", "av_40x50", "av_50x70"),
            ("av_60x80", "av_70x100", "av_80x120"),
            ("av_c12", "av_c24", "av_c36"),
            ("av_brushes",))}),
        ("Couts dropshipping (marge)", {"fields": (("cost_base", "cost_per_cm2"),
                                                   ("cost_per_color", "cost_shipping"))}),
    )
    list_display = ("__str__", "p_40x50", "p_60x80", "c_24", "brushes_price",
                    "discount_rate", "free_shipping", "updated_at")

    def has_add_permission(self, request):
        return not Pricing.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DigitalCanvas)
class DigitalCanvasAdmin(admin.ModelAdmin):
    list_display = ("uid", "apercu", "apercu_source", "title", "category", "price", "email", "source", "colors", "created_at")
    list_editable = ("title", "category", "price")
    list_filter = ("source", "category", "colors", "orientation")
    search_fields = ("uid", "email", "title", "category")
    ordering = ("-created_at",)
    fields = ("apercus", "uid", "email", "title", "category", "price", "source", "colors", "orientation",
              "width_cm", "height_cm", "created_at")
    readonly_fields = ("uid", "created_at", "apercus")
    actions = ("add_to_store", "remove_from_store", "delete_with_files")

    @admin.display(description="Toile")
    def apercu(self, obj):
        if not obj.uid:
            return "-"
        u = "/preview/img/%s/preview/" % obj.uid
        return format_html('<a href="{}" target="_blank"><img src="{}" '
                           'style="height:52px;border-radius:6px;border:1px solid #e2e8f2"></a>', u, u)

    @admin.display(description="Original")
    def apercu_source(self, obj):
        if not obj.uid:
            return "-"
        u = "/preview/img/%s/source/" % obj.uid
        return format_html('<a href="{}" target="_blank"><img src="{}" '
                           'style="height:52px;border-radius:6px;border:1px solid #e2e8f2" '
                           'onerror="this.style.display=\'none\'"></a>', u, u)

    @admin.display(description="Aperçus (original / toile / numérotée)")
    def apercus(self, obj):
        if not obj.uid:
            return "-"
        src = "/preview/img/%s/source/" % obj.uid
        prev = "/preview/img/%s/preview/" % obj.uid
        tpl = "/preview/img/%s/template/" % obj.uid
        return format_html(
            '<div style="display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap">'
            '<div><div style="font-size:.8em;color:#5b647a;margin-bottom:4px">Photo originale</div>'
            '<a href="{0}" target="_blank"><img src="{0}" style="max-height:260px;border-radius:10px;border:1px solid #e2e8f2" onerror="this.parentNode.parentNode.style.opacity=.4"></a></div>'
            '<div><div style="font-size:.8em;color:#5b647a;margin-bottom:4px">Toile coloriée</div>'
            '<a href="{1}" target="_blank"><img src="{1}" style="max-height:260px;border-radius:10px;border:1px solid #e2e8f2"></a></div>'
            '<div><div style="font-size:.8em;color:#5b647a;margin-bottom:4px">Toile numérotée</div>'
            '<a href="{2}" target="_blank"><img src="{2}" style="max-height:260px;border-radius:10px;border:1px solid #e2e8f2"></a></div>'
            '</div>', src, prev, tpl)

    @admin.action(description="Ajouter au store (bibliotheque)")
    def add_to_store(self, request, queryset):
        n = queryset.update(email="__library__", source="library")
        self.message_user(request, f"{n} modele(s) ajoute(s) au store. Renseignez titre + categorie.")

    @admin.action(description="Retirer du store")
    def remove_from_store(self, request, queryset):
        n = queryset.filter(email="__library__").update(email="", source="digital")
        self.message_user(request, f"{n} modele(s) retire(s) du store.")

    @admin.action(description="Supprimer (galerie + fichiers si numerique)")
    def delete_with_files(self, request, queryset):
        import os, shutil
        from django.conf import settings as st
        n = 0
        for c in queryset:
            uid = c.uid
            has_order = Order.objects.filter(uid=uid).exists()
            c.delete(); n += 1
            if not has_order:
                for u in (uid, uid + "-G"):
                    d = os.path.join(st.MEDIA_ROOT, "orders", u)
                    if os.path.isdir(d):
                        shutil.rmtree(d, ignore_errors=True)
        self.message_user(request, f"{n} toile(s) supprimee(s).")


@admin.register(EmailCode)
class EmailCodeAdmin(admin.ModelAdmin):
    list_display = ("email", "code", "created_at")
    search_fields = ("email",)
    ordering = ("-created_at",)


@admin.register(PrintPricing)
class PrintPricingAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Dimensions (prix de base)", {"fields": (("d_30x40","av_30x40"),("d_40x50","av_40x50"),
            ("d_50x70","av_50x70"),("d_60x80","av_60x80"),("d_70x100","av_70x100"),("d_80x120","av_80x120"))}),
        ("Matiere (supplement)", {"fields": (("m_toile","av_m_toile"),("m_alu","av_m_alu"),("m_acrylique","av_m_acrylique"))}),
        ("Cadre (supplement)", {"fields": (("f_noir","av_f_noir"),("f_bois","av_f_bois"),("f_blanc","av_f_blanc"))}),
        ("Verre", {"fields": (("av_glass","glass_price"),)}),
        ("Fournisseur impression", {"fields": ("supplier_email","supplier_url")}),
    )
    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        obj = PrintPricing.get()
        return redirect("admin:studio_printpricing_change", obj.pk)
