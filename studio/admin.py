from django.contrib import admin
from django.core.mail import EmailMessage
from django.conf import settings
from django.utils import timezone
from django import forms
from django.utils.html import format_html, escape
from django.utils.safestring import mark_safe

from .models import Discount, Order, OrderEvent, ContactMessage, ContactAttachment, Pricing, DigitalCanvas, EmailCode, PrintPricing, Supplier, MarketingAd
from . import emails

admin.site.site_header = "PaintIt ERP"
admin.site.site_title = "PaintIt ERP"
admin.site.index_title = "Gestion PaintIt"
admin.site.site_url = "/admin-hub/"
admin.site.enable_nav_sidebar = False   # remplace par le menu ERP (base_site.html)


class ActionFilter(admin.SimpleListFilter):
    """'A traiter' : memes regles (SLA) que le centre d'actions du Hub."""
    title = "a traiter"
    parameter_name = "action"

    def lookups(self, request, model_admin):
        from . import erp
        return [(k, v[1]) for k, v in erp.action_querysets().items()]

    def queryset(self, request, queryset):
        from . import erp
        qs = erp.action_querysets().get(self.value())
        return queryset.filter(pk__in=qs[0].values("pk")) if qs else queryset


class OrderEventInline(admin.TabularInline):
    model = OrderEvent
    extra = 0
    can_delete = False
    fields = ("at", "kind", "status_label", "text", "user")
    readonly_fields = fields
    verbose_name_plural = "Historique (journal)"

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Statut")
    def status_label(self, obj):
        return dict(Order.STATUS_CHOICES).get(obj.status, obj.status or "")


def _log(order, kind, text, request=None, status=""):
    OrderEvent.objects.create(order=order, kind=kind, text=text[:300], status=status,
                              user=request.user.get_username() if request else "")


def _set_status(order, status, request):
    order.status = status
    order._erp_user = request.user.get_username()
    order.save(update_fields=["status", "status_changed_at"])


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        # La liste ERP remplace la table Django (?django=1 pour la table, utile aux actions avancees)
        from django.shortcuts import redirect
        from urllib.parse import urlencode
        if request.method == "GET" and not request.GET.get("django"):
            g = request.GET
            p = {}
            if g.get("action"):
                p["action"] = g["action"]
            if g.get("status__exact"):
                p["status"] = g["status__exact"]
            if g.get("q"):
                p["q"] = g["q"]
            if g.get("supplier__id__exact"):
                p["supplier"] = g["supplier__id__exact"]
            return redirect("/admin-hub/orders/" + ("?" + urlencode(p) if p else ""))
        if "django" in request.GET:   # parametre de contournement : inconnu des filtres Django
            g = request.GET.copy(); g.pop("django"); request.GET = g
        return super().changelist_view(request, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        # La fiche ERP remplace le formulaire (?django=1 pour le formulaire complet)
        from django.shortcuts import redirect
        if request.method == "GET" and not request.GET.get("django"):
            return redirect("studio:erp_order", pk=object_id)
        return super().change_view(request, object_id, form_url, extra_context)

    def response_change(self, request, obj):
        from django.shortcuts import redirect
        if "_continue" in request.POST:
            return super().response_change(request, obj)
        return redirect("studio:erp_order", pk=obj.pk)

    def get_fieldsets(self, request, obj=None):
        fs = super().get_fieldsets(request, obj)
        try:
            first_fields = [f for f in fs[0][1]["fields"] if f != "fichiers"]
            rest = list(fs[1:])
            return [("Fichiers associes", {"fields": ("fichiers",)}),
                    (None, {"fields": first_fields})] + rest
        except Exception:
            return fs

    @admin.display(description="Fichiers associes")
    def fichiers(self, obj):
        import os, glob
        from django.conf import settings
        if not obj or not obj.uid:
            return "-"
        d = os.path.join(settings.MEDIA_ROOT, "orders", obj.uid)
        if not os.path.isdir(d):
            return mark_safe('<span style="color:#8a97b4">Aucun fichier (uid %s)</span>' % obj.uid)
        parts = ['<div style="display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start">']
        for kind, label in [("preview", "Toile coloriee"), ("template", "Toile numerotee"),
                            ("digipaint", "Jouable"), ("source", "Photo originale")]:
            u = "/preview/img/%s/%s/" % (obj.uid, kind)
            parts.append('<div style="text-align:center"><div style="font-size:.78em;color:#5b647a;margin-bottom:4px">%s</div>'
                         '<a href="%s" target="_blank"><img src="%s" style="max-height:150px;border:1px solid #e2e8f2;border-radius:8px" '
                         'onerror="this.parentNode.style.display=\'none\'"></a></div>' % (label, u, u))
        parts.append('</div>')
        # Fichiers telechargeables (tiff, svg, poster, palette, colors.json)
        dl = []
        for f in sorted(glob.glob(os.path.join(d, "*.tiff")) + glob.glob(os.path.join(d, "*.svg"))
                        + glob.glob(os.path.join(d, "*_palette.png")) + glob.glob(os.path.join(d, "*colors.json"))):
            name = os.path.basename(f)
            u = "/files/%s/%s" % (obj.uid, name)   # vue staff (media/orders non public)
            kb = os.path.getsize(f) // 1024
            tag = "TIFF" if name.endswith(".tiff") else ("SVG" if name.endswith(".svg") else name.split(".")[-1].upper())
            dl.append('<a class="button" href="%s" download style="margin:3px 8px 3px 0;display:inline-block">%s , %s (%d ko)</a>'
                      % (u, tag, name, kb))
        if dl:
            parts.append('<div style="margin-top:12px"><div style="font-size:.78em;color:#5b647a;margin-bottom:6px">'
                         'Fichiers fournisseur / export</div>' + "".join(dl) + '</div>')
        return mark_safe("".join(parts))

    @admin.action(description="Generer les fichiers fournisseur (.tiff)")
    def fichiers_fournisseur(self, request, queryset):
        from .pipeline import export_tiff
        n = 0
        for o in queryset:
            uid = getattr(o, "uid", None) or (o.session_uid if hasattr(o, "session_uid") else None)
            if not uid:
                continue
            try:
                made = export_tiff(uid)
                n += len(made)
            except Exception as exc:
                self.message_user(request, "%s : %s" % (uid, exc), level="error")
        self.message_user(request, "%d fichier(s) .tiff genere(s)." % n)

    list_display = ("uid", "status_badge", "since_col", "product_col", "total", "benefit_col",
                    "tracking_col", "customer_col", "country", "created_at")
    list_filter = (ActionFilter, "status", "supplier", "country", "lang", "carrier", "colors", "brushes")
    inlines = (OrderEventInline,)
    save_on_top = True
    search_fields = ("uid", "customer_name", "customer_email", "discount_code",
                     "supplier_ref", "tracking_number")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 25
    readonly_fields = ("uid", "created_at", "status_changed_at", "cost", "benefit_col", "supplier_ref",
                       "feedback_sent", "fichiers")
    actions = ("send_to_supplier", "fichiers_fournisseur", "mark_fulfilled", "mark_shipped", "mark_delivered",
               "mark_failed", "send_feedback")

    @admin.action(description="Transmettre au fournisseur (API / e-mail)")
    def send_to_supplier(self, request, queryset):
        from .views import _notify_supplier, _order_from_row, _shipping_from_row
        ok = ko = 0
        for o in queryset:
            if _notify_supplier(_order_from_row(o), _shipping_from_row(o), sup=o.supplier,
                                user=request.user.get_username()):
                ok += 1
                if o.status == Order.PAID:
                    _set_status(o, Order.FULFILLED, request)
            else:
                ko += 1
        self.message_user(request, "%d transmise(s), %d echec(s) (voir le journal de la commande)." % (ok, ko),
                          level="warning" if ko else "info")
    fieldsets = (
        ("Commande", {"fields": (("uid", "status"), ("created_at", "status_changed_at"), ("supplier", "supplier_ref"), "lang")}),
        ("Notes internes", {"fields": ("notes",),
                            "description": "Visible uniquement en interne ; chaque modification est tracee dans l'historique."}),
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

    @admin.display(description="Depuis", ordering="status_changed_at")
    def since_col(self, obj):
        if not obj.status_changed_at:
            return "\u2014"
        d = (timezone.now() - obj.status_changed_at)
        txt = "%d j" % d.days if d.days else "%d h" % (d.seconds // 3600)
        late = obj.status in (Order.PAID, Order.FULFILLED, Order.SHIPPED) and d.days >= 7
        return format_html('<span style="color:{};font-weight:{}">{}</span>',
                           "#c0392b" if late else "#5b647a", 700 if late else 400, txt)

    @admin.display(description="Client", ordering="customer_email")
    def customer_col(self, obj):
        return format_html('{}<br><a href="/admin/studio/order/?q={}" style="font-size:.85em">{}</a>',
                           obj.customer_name or "", obj.customer_email, obj.customer_email)

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
        obj._erp_user = request.user.get_username()
        super().save_model(request, obj, form, change)
        if change and "notes" in form.changed_data:
            _log(obj, "note", "Notes modifiees : " + (obj.notes or "(vide)").strip().replace("\n", " "), request)
        for f in ("tracking_number", "carrier"):
            if change and f in form.changed_data and getattr(obj, f):
                _log(obj, "action", "%s : %s" % (form.fields[f].label, getattr(obj, f)), request)
        # E-mail d'avis automatique quand la commande passe a "Livree".
        if obj.status == Order.DELIVERED and not obj.feedback_sent:
            emails.send_feedback_request(obj)
            Order.objects.filter(pk=obj.pk).update(feedback_sent=True)
            _log(obj, "email", "Demande d'avis envoyee", request)

    def _bulk_status(self, request, queryset, status, label):
        n = 0
        for o in queryset.exclude(status=status):
            _set_status(o, status, request); n += 1
        self.message_user(request, f"{n} commande(s) : {label}.")

    @admin.action(description="Marquer en production (envoyee au fournisseur)")
    def mark_fulfilled(self, request, queryset):
        self._bulk_status(request, queryset, Order.FULFILLED, "en production")

    @admin.action(description="Marquer comme expediee")
    def mark_shipped(self, request, queryset):
        missing = queryset.filter(tracking_number="").count()
        self._bulk_status(request, queryset, Order.SHIPPED, "expediee(s)")
        if missing:
            self.message_user(request, f"{missing} commande(s) sans n° de suivi : pensez a le renseigner.",
                              level="warning")

    @admin.action(description="Marquer en echec")
    def mark_failed(self, request, queryset):
        self._bulk_status(request, queryset, Order.FAILED, "en echec")

    @admin.action(description="Marquer comme livree + demander un avis")
    def mark_delivered(self, request, queryset):
        sent = 0
        for o in queryset:
            if o.status != Order.DELIVERED:
                _set_status(o, Order.DELIVERED, request)
            if not o.feedback_sent:
                emails.send_feedback_request(o)
                Order.objects.filter(pk=o.pk).update(feedback_sent=True)
                _log(o, "email", "Demande d'avis envoyee", request)
                sent += 1
        self.message_user(request, f"{queryset.count()} livree(s), {sent} e-mail(s) d'avis envoye(s).")

    @admin.action(description="Envoyer la demande d'avis (e-mail)")
    def send_feedback(self, request, queryset):
        sent = 0
        for o in queryset:
            emails.send_feedback_request(o)
            Order.objects.filter(pk=o.pk).update(feedback_sent=True)
            _log(o, "email", "Demande d'avis envoyee", request)
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


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data if d]
        return [single(data, initial)] if data else []


class ContactMessageAdminForm(forms.ModelForm):
    reply_attachments = MultipleFileField(
        required=False, label="Pieces jointes a la reponse")

    class Meta:
        model = ContactMessage
        fields = ("answer", "answered")


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        if request.method == "GET" and "django" not in request.GET:
            box = {"0": "open", "1": "done"}.get(request.GET.get("answered__exact", ""), "all" if request.GET else "open")
            q = request.GET.get("q", "")
            return redirect("/admin-hub/messages/?box=%s%s" % (box, "&q=" + q if q else ""))
        if "django" in request.GET:
            g = request.GET.copy(); g.pop("django"); request.GET = g
        return super().changelist_view(request, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        from django.shortcuts import redirect
        if request.method == "GET" and "django" not in request.GET:
            return redirect("studio:erp_inbox_msg", pk=object_id)
        return super().change_view(request, object_id, form_url, extra_context)

    form = ContactMessageAdminForm
    list_display = ("subject", "name", "email", "has_files", "answered", "age_col", "created_at", "answered_at")

    @admin.display(description="Attente", ordering="created_at")
    def age_col(self, obj):
        if obj.answered:
            return "\u2014"
        h = int((timezone.now() - obj.created_at).total_seconds() // 3600)
        return format_html('<b style="color:{}">{}</b>', "#c0392b" if h >= 24 else "#a86a00",
                           "%d j" % (h // 24) if h >= 48 else "%d h" % h)
    list_filter = ("answered",)
    search_fields = ("name", "email", "subject", "message")
    ordering = ("-created_at",)
    readonly_fields = ("conversation", "name", "email", "subject", "message",
                       "attachments_preview", "created_at", "answered_at")
    fieldsets = (
        ("Conversation", {"fields": ("conversation",)}),
        ("Message recu", {"fields": (("name", "email"), "subject", "message",
                                     "attachments_preview", "created_at")}),
        ("Repondre", {"fields": ("answer", "reply_attachments",
                                 ("answered", "answered_at"))}),
    )

    @admin.display(description="PJ", boolean=True)
    def has_files(self, obj):
        return obj.attachments.exists()

    @admin.display(description="Pieces jointes recues")
    def attachments_preview(self, obj):
        if not obj.pk:
            return "-"
        atts = list(obj.attachments.all())
        if not atts:
            return mark_safe('<span style="color:#8a97b4">Aucune piece jointe</span>')
        parts = ['<div style="display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start">']
        for a in atts:
            url = escape(a.file.url)
            ct = (a.content_type or "").lower()
            name = escape(a.original_name or a.file.name.split("/")[-1])
            if ct.startswith("image/"):
                inner = (f'<a href="{url}" target="_blank"><img src="{url}" '
                         f'style="max-height:240px;max-width:300px;border:1px solid #e2e8f2;border-radius:8px"></a>')
            elif "pdf" in ct:
                inner = (f'<embed src="{url}" type="application/pdf" '
                         f'style="width:300px;height:380px;border:1px solid #e2e8f2;border-radius:8px">'
                         f'<div style="margin-top:4px"><a href="{url}" target="_blank">Ouvrir le PDF</a></div>')
            else:
                inner = f'<a class="button" href="{url}" target="_blank" download>{name}</a>'
            parts.append(f'<div style="text-align:center"><div style="font-size:.8em;color:#5b647a;margin-bottom:4px">{name}</div>{inner}</div>')
        parts.append('</div>')
        return mark_safe("".join(parts))

    @admin.display(description="Conversation avec ce contact")
    def conversation(self, obj):
        if not obj.pk or not obj.email:
            return "-"
        msgs = list(ContactMessage.objects.filter(email=obj.email).order_by("created_at"))
        if len(msgs) <= 1 and not (obj.answer or "").strip():
            return mark_safe('<span style="color:#8a97b4">Premier echange avec ce contact.</span>')

        def bubble(side, who, when, subject, body):
            align = "flex-start" if side == "in" else "flex-end"
            bg = "#eef2fb" if side == "in" else "#e7f6ec"
            head = escape(who)
            if when:
                head = head + " - " + escape(when.strftime("%d/%m/%Y %H:%M"))
            sub = (f'<div style="font-weight:700;color:#12224f;margin-bottom:2px">{escape(subject)}</div>'
                   if subject else "")
            txt = escape(body or "").replace("\n", "<br>")
            return (f'<div style="display:flex;justify-content:{align};margin:6px 0">'
                    f'<div style="max-width:80%;background:{bg};border:1px solid #e2e8f2;border-radius:12px;padding:8px 12px">'
                    f'<div style="font-size:.72em;color:#5b647a;margin-bottom:3px">{head}</div>{sub}'
                    f'<div style="font-size:.92em;color:#233">{txt}</div></div></div>')

        rows = ['<div style="max-width:820px">']
        for m in msgs:
            mark = " (message courant)" if m.pk == obj.pk else ""
            rows.append(bubble("in", (m.name or m.email) + mark, m.created_at, m.subject, m.message))
            if (m.answer or "").strip():
                rows.append(bubble("out", "PaintIt", m.answered_at, "", m.answer))
        rows.append('</div>')
        return mark_safe("".join(rows))

    def save_model(self, request, obj, form, change):
        reply_files = form.cleaned_data.get("reply_attachments") or []
        # Repondre depuis l'admin : texte et/ou pieces jointes -> e-mail au client.
        if ((obj.answer or "").strip() or reply_files) and not obj.answered:
            mail = EmailMessage(
                subject="Re: %s" % obj.subject,
                body=obj.answer or "",
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[obj.email],
            )
            for f in reply_files:
                try:
                    mail.attach(f.name, f.read(), getattr(f, "content_type", None) or None)
                except Exception:
                    pass
            mail.send(fail_silently=True)
            obj.answered = True
            obj.answered_at = timezone.now()
        super().save_model(request, obj, form, change)


# Pricing retire du Django admin : tout se gere via /admin-tarifs/

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

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PrintPricing)
class PrintPricingAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Dimensions (prix de base)", {"fields": (("d_30x40","av_30x40"),("d_40x50","av_40x50"),
            ("d_50x70","av_50x70"),("d_60x80","av_60x80"),("d_70x100","av_70x100"),("d_80x120","av_80x120"))}),
        ("Matiere (supplement)", {"fields": (("m_toile","av_m_toile"),("m_alu","av_m_alu"),("m_bois","av_m_bois"))}),
        ("Cadre (supplement)", {"fields": (("f_noir","av_f_noir"),("f_bois","av_f_bois"),("f_blanc","av_f_blanc"))}),
        ("Verre", {"fields": (("av_glass","glass_price"),)}),
        ("Fournisseur impression", {"fields": ("supplier_email","supplier_url")}),
    )
    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        obj = PrintPricing.get()
        return redirect("admin:studio_printpricing_change", obj.pk)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect
        if request.GET or request.method == "POST":
            return super().changelist_view(request, extra_context)
        return redirect("studio:erp_suppliers")   # le tableau de bord remplace la liste

    def _back(self, request, obj, default):
        from django.shortcuts import redirect
        if "_continue" in request.POST or "_addanother" in request.POST:
            return default
        return redirect("studio:erp_supplier", pk=obj.pk)

    def response_add(self, request, obj, post_url_continue=None):
        return self._back(request, obj, super().response_add(request, obj, post_url_continue))

    def response_change(self, request, obj):
        return self._back(request, obj, super().response_change(request, obj))

    list_display = ("name", "active", "checkout", "integration", "email", "lead_time_days", "priority")
    list_editable = ("active", "checkout", "priority")
    list_filter = ("active", "checkout", "integration")
    search_fields = ("name", "email", "contact_name")
    save_on_top = True
    fieldsets = (
        ("Fournisseur", {"fields": (("name", "active", "priority"), "checkout")}),
        ("Contact", {"fields": ("contact_name", ("contact_email", "phone"), "address")}),
        ("Finance", {"fields": (("bank_name", "iban", "bic"), "pricing")}),
        ("Process", {"fields": (("lead_time_days", "incoterms"), "production_notes", "terms")}),
        ("Integration (plug & play)", {"fields": ("integration", "email", ("api_url", "api_key")),
            "description": "E-mail : commande + liens fichiers par mail. API : POST JSON (Bearer cle)."}),
        ("Fichiers a transmettre", {"fields": (
            ("want_source", "want_order_json"),
            ("want_template_svg", "want_template_tiff"),
            ("want_preview_svg", "want_poster")),
            "description": "Toile numerotee, apercu colorie, poster, et order JSON "
                           "(consignee / order information / color specifications)."}),
        ("Notes", {"fields": ("notes",), "classes": ("collapse",)}),
    )


@admin.register(MarketingAd)
class MarketingAdAdmin(admin.ModelAdmin):
    list_display = ("image_uid", "variant", "kind", "campaign", "views", "clicks", "ctr_col", "active", "created_at")
    list_filter = ("active", "fmt", "kind", "campaign")
    search_fields = ("image_uid", "campaign", "variant", "token")
    list_editable = ("active",)
    readonly_fields = ("token", "image_uid", "kind", "fmt", "file_name", "views", "clicks", "created_at", "links")
    fields = ("links", ("image_uid", "variant", "kind", "fmt"), "campaign", "target_url", "texts",
              ("views", "clicks"), "active", "token", "file_name", "created_at")

    @admin.display(description="CTR")
    def ctr_col(self, obj):
        return "%s %%" % obj.ctr if obj.ctr is not None else "\u2014"

    @admin.display(description="Liens")
    def links(self, obj):
        site = settings.SITE_URL
        return format_html('Clic traque : <code>{}/go/{}/</code><br>Pub publique : <code>{}/m/{}/</code><br>'
                           '<a href="/admin-hub/marketing/">Galerie A/B</a>', site, obj.token, site, obj.token)
