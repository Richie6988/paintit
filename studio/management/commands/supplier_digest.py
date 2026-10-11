"""E-mail quotidien « commandes a importer dans l'ERP du fournisseur » (MangoERP...) :
un seul fichier Excel (au format du modele du fournisseur) avec toutes les commandes transmises
depuis le dernier envoi. A planifier en cron, ex. chaque matin :
    0 8 * * * cd /home/paintit/Django && .venv/bin/python manage.py supplier_digest
"""
from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.utils import timezone

from studio import xlsx_bridge
from studio.models import CompanyInfo, Order, OrderEvent, Supplier

MARK = "Inclus dans l'export Excel"


class Command(BaseCommand):
    help = "Envoie l'Excel quotidien des commandes a importer dans l'ERP de chaque fournisseur."

    def add_arguments(self, parser):
        parser.add_argument("--to", default="", help="Destinataire (defaut : e-mail de contact de la page Legal)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        to = opts["to"] or CompanyInfo.get().email
        for sup in Supplier.objects.filter(active=True):
            if not (sup.offer or {}).get("xlsx"):
                continue
            done = OrderEvent.objects.filter(kind="action", text__startswith=MARK).values("order_id")
            orders = list(Order.objects.filter(supplier=sup, status__in=(Order.PAID, Order.FULFILLED))
                          .exclude(pk__in=done).order_by("created_at"))
            if not orders:
                self.stdout.write("%s : rien a importer" % sup.name)
                continue
            self.stdout.write("%s : %d commande(s) -> %s" % (sup.name, len(orders), to))
            if opts["dry_run"]:
                continue
            day = timezone.localdate().strftime("%Y-%m-%d")
            body = ["Commandes a importer dans l'ERP de %s (%d) :" % (sup.name, len(orders)), ""]
            body += ["- %s · %s · %d couleurs%s · %s" % (o.uid, o.format_label, o.colors, " · pinceaux" if o.brushes else "",
                                                        o.customer_name) for o in orders]
            body += ["", "MangoERP : Commandes (订单) -> Creer une commande manuelle (手工创建订单) -> Importer (导入订单)",
                     "-> boutique personnalisee PaintIt -> choisir le fichier joint.",
                     "Les fichiers de production (ZIP + PDF) sont en lien dans la colonne Remarques (备注).",
                     "", "Suivi des expeditions : exporter les commandes expediees de Mango puis "
                     "« Importer les n° de suivi » sur la fiche fournisseur : %s/admin-hub/suppliers/%d/" % (settings.SITE_URL, sup.pk)]
            msg = EmailMessage("PaintIt · %d commande(s) a importer · %s · %s" % (len(orders), sup.name, day),
                               "\n".join(body), settings.DEFAULT_FROM_EMAIL, [to])
            msg.attach("paintit-%s-%s.xlsx" % (sup.name.lower().replace(" ", "-"), day), xlsx_bridge.build_xlsx(sup, orders),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            msg.send(fail_silently=False)
            for o in orders:
                OrderEvent.objects.create(order=o, kind="action", user="export auto",
                                          text="%s du %s (%s)" % (MARK, day, sup.name))
