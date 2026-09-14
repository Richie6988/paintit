from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.conf import settings


class Command(BaseCommand):
    help = "Envoie des e-mails de test (verifier SMTP + rendu) a une adresse."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Adresse de destination")
        parser.add_argument("--lang", default="fr")

    def handle(self, *args, **opts):
        to = opts["email"]
        lang = opts["lang"]
        from studio import emails
        ok = []

        # 1) Test SMTP brut
        try:
            n = send_mail("PaintIt , test SMTP", "Test d'envoi PaintIt (SMTP OK).",
                          settings.DEFAULT_FROM_EMAIL, [to], fail_silently=False)
            ok.append(f"smtp ({n})")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"SMTP KO: {e}"))
            self.stdout.write(self.style.WARNING(
                "Verifie EMAIL_HOST/PORT/USER/PASSWORD (systemd) et EMAIL_USE_SSL."))
            return

        # 2) Code de verification galerie
        try:
            emails.send_code(to, "123456", lang); ok.append("code")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"code KO: {e}"))

        # 3) Confirmation de commande (donnees factices)
        order = {"uid": "TEST1234", "format_label": "40 x 50 cm", "orientation": "portrait",
                 "colors": 24, "price": 42.9, "total": 42.9, "cost": 0, "lang": lang,
                 "discount": None, "brushes": False, "brushes_amount": 0}
        shipping = {"full_name": "Client Test", "email": to, "phone_code": "+33", "phone": "0612345678",
                    "address1": "1 rue de Test", "address2": "", "postal_code": "75000",
                    "city": "Paris", "country": "France"}
        try:
            emails.send_order_confirmation(order, shipping); ok.append("confirmation")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"confirmation KO: {e}"))

        # 4) Galerie (liens)
        try:
            emails.send_gallery(to, [{"uid": "TEST1234", "colors": 24}], lang); ok.append("gallery")
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"gallery KO: {e}"))

        self.stdout.write(self.style.SUCCESS(f"Envoyes a {to} : {', '.join(ok) or 'aucun'}"))
