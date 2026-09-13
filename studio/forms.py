from django import forms
from django.utils.translation import gettext_lazy as _

from . import address

# Formats standards, faciles a imprimer (ratio court x long, en cm).
FORMATS = {
    "30x40":   (30.0, 40.0, "30 x 40 cm"),
    "40x50":   (40.0, 50.0, "40 x 50 cm"),
    "50x70":   (50.0, 70.0, "50 x 70 cm"),
    "60x80":   (60.0, 80.0, "60 x 80 cm"),
    "70x100":  (70.0, 100.0, "70 x 100 cm"),
    "80x120":  (80.0, 120.0, "80 x 120 cm"),
}
FORMAT_CHOICES = [(k, v[2]) for k, v in FORMATS.items()]
ORIENTATION_CHOICES = [("portrait", _("Portrait")), ("paysage", _("Paysage"))]
COLOR_CHOICES = [("12", _("12 couleurs")), ("24", _("24 couleurs")), ("36", _("36 couleurs"))]

PHONE_CODES = [
    ("+33", "France (+33)"), ("+32", "Belgique (+32)"), ("+41", "Suisse (+41)"),
    ("+352", "Luxembourg (+352)"), ("+49", "Allemagne (+49)"), ("+34", "Espagne (+34)"),
    ("+39", "Italie (+39)"), ("+351", "Portugal (+351)"), ("+31", "Pays-Bas (+31)"),
    ("+43", "Autriche (+43)"), ("+353", "Irlande (+353)"), ("+45", "Danemark (+45)"),
    ("+46", "Suède (+46)"), ("+47", "Norvège (+47)"), ("+358", "Finlande (+358)"),
    ("+48", "Pologne (+48)"), ("+420", "Rép. tchèque (+420)"), ("+30", "Grèce (+30)"),
    ("+44", "Royaume-Uni (+44)"), ("+1", "USA/Canada (+1)"), ("+61", "Australie (+61)"),
    ("+81", "Japon (+81)"), ("+55", "Brésil (+55)"), ("+212", "Maroc (+212)"),
    ("+216", "Tunisie (+216)"), ("+225", "Côte d'Ivoire (+225)"), ("+221", "Sénégal (+221)"),
    ("+971", "Émirats (+971)"), ("+65", "Singapour (+65)"),
]


def dimensions(format_key, orientation):
    short, long_, _label = FORMATS[format_key]
    if orientation == "paysage":
        return long_, short
    return short, long_


class UploadForm(forms.Form):
    photo = forms.ImageField(label=_("Votre photo"), required=False)
    orientation = forms.ChoiceField(choices=ORIENTATION_CHOICES, initial="portrait",
                                    widget=forms.RadioSelect, label=_("Orientation"))
    colors = forms.ChoiceField(choices=COLOR_CHOICES, initial="24",
                               widget=forms.RadioSelect, label=_("Nombre de couleurs"))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .models import Pricing
        p = Pricing.get()
        cols = [n for n in (12, 24, 36) if getattr(p, "av_c%d" % n, True)] or [24]
        self.fields["colors"].choices = [(v, l) for (v, l) in COLOR_CHOICES if int(v) in cols]


class DeliveryForm(forms.Form):
    full_name = forms.CharField(label=_("Nom complet"), max_length=120)
    email = forms.EmailField(label=_("E-mail"))
    phone_code = forms.ChoiceField(choices=PHONE_CODES, initial="+33", label=_("Indicatif"))
    phone = forms.CharField(label=_("Téléphone"), max_length=20)
    address1 = forms.CharField(label=_("Adresse"), max_length=200)
    address2 = forms.CharField(label=_("Complément"), max_length=200, required=False)
    postal_code = forms.CharField(label=_("Code postal"), max_length=20)
    city = forms.CharField(label=_("Ville"), max_length=120)
    country = forms.ChoiceField(choices=address.COUNTRY_CHOICES, initial="France", label=_("Pays"))

    def clean(self):
        cleaned = super().clean()
        errors = address.validate(cleaned)
        if errors:
            raise forms.ValidationError(errors)
        return cleaned


class ContactForm(forms.Form):
    name = forms.CharField(label=_("Nom"), max_length=120)
    email = forms.EmailField(label=_("E-mail"))
    subject = forms.CharField(label=_("Sujet"), max_length=140)
    message = forms.CharField(label=_("Message"), widget=forms.Textarea)
