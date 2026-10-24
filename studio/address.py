"""Verification d'adresse a la commande.

Validation locale : champs requis, format de code postal selon le pays, telephone
plausible. Hook optionnel vers une API externe (ADDRESS_API_URL) si configuree.
"""
import re

from django.conf import settings

try:
    import requests
except ImportError:
    requests = None

# Pays -> (code ISO, regex de code postal, normaliseur eventuel)
ANY = r"^.{2,12}$"
COUNTRIES = {
    "France": ("FR", r"^\d{5}$"),
    "Belgique": ("BE", r"^\d{4}$"),
    "Suisse": ("CH", r"^\d{4}$"),
    "Luxembourg": ("LU", r"^\d{4}$"),
    "Allemagne": ("DE", r"^\d{5}$"),
    "Espagne": ("ES", r"^\d{5}$"),
    "Italie": ("IT", r"^\d{5}$"),
    "Portugal": ("PT", r"^\d{4}-\d{3}$"),
    "Pays-Bas": ("NL", r"^\d{4} ?[A-Z]{2}$"),
    "Autriche": ("AT", r"^\d{4}$"),
    "Irlande": ("IE", ANY),
    "Danemark": ("DK", r"^\d{4}$"),
    "Suede": ("SE", r"^\d{3} ?\d{2}$"),
    "Norvege": ("NO", r"^\d{4}$"),
    "Finlande": ("FI", r"^\d{5}$"),
    "Pologne": ("PL", r"^\d{2}-\d{3}$"),
    "Republique tcheque": ("CZ", r"^\d{3} ?\d{2}$"),
    "Grece": ("GR", r"^\d{3} ?\d{2}$"),
    "Royaume-Uni": ("GB", r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$"),
    "Irlande du Nord": ("GB", r"^BT\d{1,2} ?\d[A-Z]{2}$"),
    "Canada": ("CA", r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$"),
    "Etats-Unis": ("US", r"^\d{5}(-\d{4})?$"),
    "Australie": ("AU", r"^\d{4}$"),
    "Japon": ("JP", r"^\d{3}-?\d{4}$"),
    "Bresil": ("BR", r"^\d{5}-?\d{3}$"),
    "Maroc": ("MA", r"^\d{5}$"),
    "Tunisie": ("TN", r"^\d{4}$"),
    "Cote d'Ivoire": ("CI", ANY),
    "Senegal": ("SN", ANY),
    "Emirats arabes unis": ("AE", ANY),
    "Singapour": ("SG", r"^\d{6}$"),
    "Slovaquie": ("SK", r"^\d{3} ?\d{2}$"),
    "Slovenie": ("SI", r"^\d{4}$"),
    "Croatie": ("HR", r"^\d{5}$"),
    "Hongrie": ("HU", r"^\d{4}$"),
    "Roumanie": ("RO", r"^\d{6}$"),
    "Bulgarie": ("BG", r"^\d{4}$"),
    "Chypre": ("CY", r"^\d{4}$"),
    "Malte": ("MT", r"^[A-Z]{3} ?\d{4}$"),
    "Estonie": ("EE", r"^\d{5}$"),
    "Lettonie": ("LV", r"^(LV-)?\d{4}$"),
    "Lituanie": ("LT", r"^(LT-)?\d{5}$"),
    "Nouvelle-Zelande": ("NZ", r"^\d{4}$"),
}

# Pays desservis par le fournisseur (en attendant sa liste complete) : UE + Etats-Unis + Royaume-Uni + Nouvelle-Zelande.
# Hors liste : pays absent du formulaire et commande refusee.
EU = {"FR", "BE", "LU", "DE", "ES", "IT", "PT", "NL", "AT", "IE", "DK", "SE", "FI", "PL", "CZ", "GR", "SK", "SI",
      "HR", "HU", "RO", "BG", "CY", "MT", "EE", "LV", "LT"}
SHIP_ISO = EU | {"US", "GB", "NZ"}


def ships_to(country):
    spec = COUNTRIES.get(country)
    return bool(spec) and spec[0] in SHIP_ISO


COUNTRY_CHOICES = [(c, c) for c in COUNTRIES if ships_to(c)]


def validate(shipping):
    """Renvoie une liste de messages d'erreur (vide si l'adresse est valide)."""
    errors = []
    for field in ("full_name", "address1", "postal_code", "city", "country"):
        if not str(shipping.get(field, "")).strip():
            errors.append("Champ requis manquant : %s." % field)

    country = shipping.get("country", "")
    if country and not ships_to(country):
        from django.utils.translation import gettext as _
        errors.append(_("Désolé, nous ne livrons pas encore ce pays."))
    postal = str(shipping.get("postal_code", "")).strip().upper()
    spec = COUNTRIES.get(country)
    if spec and postal and not re.match(spec[1], postal):
        errors.append("Code postal invalide pour %s." % country)

    phone = re.sub(r"\D", "", str(shipping.get("phone", "")))
    if len(phone) < 6:
        errors.append("Numero de telephone invalide.")

    # Verification externe optionnelle.
    url = getattr(settings, "ADDRESS_API_URL", "")
    if url and requests and not errors:
        try:
            r = requests.post(url, json={
                "address1": shipping.get("address1", ""),
                "address2": shipping.get("address2", ""),
                "postal_code": postal, "city": shipping.get("city", ""),
                "country_code": spec[0] if spec else "",
            }, timeout=8)
            if r.status_code == 200 and r.json().get("valid") is False:
                errors.append("Adresse non reconnue par la verification postale.")
        except Exception:
            pass  # ne bloque pas si le service est indisponible
    return errors
