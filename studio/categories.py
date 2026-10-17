"""Traduction des categories de la galerie (saisies en francais dans le catalogue) pour le site en 5 langues.

Les categories sont du texte libre : on reconnait les noms usuels (themes de la banque d'images, dossiers de
galerie...) sans tenir compte des accents ni de la casse. Une categorie inconnue s'affiche telle quelle, et le
catalogue la signale pour qu'on choisisse plutot un nom de la liste.
"""
import unicodedata

# francais -> (en, de, es, it)
CATEGORIES = {
    "Animaux": ("Animals", "Tiere", "Animales", "Animali"),
    "Animaux de compagnie": ("Pets", "Haustiere", "Mascotas", "Animali domestici"),
    "Chats": ("Cats", "Katzen", "Gatos", "Gatti"),
    "Chiens": ("Dogs", "Hunde", "Perros", "Cani"),
    "Chevaux": ("Horses", "Pferde", "Caballos", "Cavalli"),
    "Oiseaux": ("Birds", "Vögel", "Aves", "Uccelli"),
    "Papillons & insectes": ("Butterflies & insects", "Schmetterlinge & Insekten", "Mariposas e insectos", "Farfalle e insetti"),
    "Papillons": ("Butterflies", "Schmetterlinge", "Mariposas", "Farfalle"),
    "Fleurs": ("Flowers", "Blumen", "Flores", "Fiori"),
    "Nature": ("Nature", "Natur", "Naturaleza", "Natura"),
    "Paysages": ("Landscapes", "Landschaften", "Paisajes", "Paesaggi"),
    "Montagne": ("Mountains", "Berge", "Montaña", "Montagna"),
    "Mer & plages": ("Sea & beaches", "Meer & Strände", "Mar y playas", "Mare e spiagge"),
    "Mer": ("Sea", "Meer", "Mar", "Mare"),
    "Plages": ("Beaches", "Strände", "Playas", "Spiagge"),
    "Fonds marins": ("Underwater", "Unterwasserwelt", "Fondos marinos", "Fondali marini"),
    "Couchers de soleil": ("Sunsets", "Sonnenuntergänge", "Atardeceres", "Tramonti"),
    "Villes & voyages": ("Cities & travel", "Städte & Reisen", "Ciudades y viajes", "Città e viaggi"),
    "Villes": ("Cities", "Städte", "Ciudades", "Città"),
    "Voyages": ("Travel", "Reisen", "Viajes", "Viaggi"),
    "Architecture": ("Architecture", "Architektur", "Arquitectura", "Architettura"),
    "Espace": ("Space", "Weltraum", "Espacio", "Spazio"),
    "Cuisine & fruits": ("Food & fruit", "Essen & Obst", "Cocina y frutas", "Cucina e frutta"),
    "Cuisine": ("Food", "Essen", "Cocina", "Cucina"),
    "Saisons": ("Seasons", "Jahreszeiten", "Estaciones", "Stagioni"),
    "Automne": ("Autumn", "Herbst", "Otoño", "Autunno"),
    "Hiver": ("Winter", "Winter", "Invierno", "Inverno"),
    "Printemps": ("Spring", "Frühling", "Primavera", "Primavera"),
    "Été": ("Summer", "Sommer", "Verano", "Estate"),
    "Noël": ("Christmas", "Weihnachten", "Navidad", "Natale"),
    "Abstrait": ("Abstract", "Abstrakt", "Abstracto", "Astratto"),
    "Art": ("Art", "Kunst", "Arte", "Arte"),
    "Tableaux célèbres": ("Famous paintings", "Berühmte Gemälde", "Cuadros famosos", "Dipinti famosi"),
    "Portraits": ("Portraits", "Porträts", "Retratos", "Ritratti"),
    "Enfants": ("Kids", "Kinder", "Niños", "Bambini"),
    "Véhicules": ("Vehicles", "Fahrzeuge", "Vehículos", "Veicoli"),
    "Motos": ("Motorbikes", "Motorräder", "Motos", "Moto"),
    "Voitures": ("Cars", "Autos", "Coches", "Auto"),
    "Sport": ("Sport", "Sport", "Deporte", "Sport"),
    "Musique": ("Music", "Musik", "Música", "Musica"),
    "Fantastique": ("Fantasy", "Fantasy", "Fantasía", "Fantasy"),
    "Galerie": ("Gallery", "Galerie", "Galería", "Galleria"),
    "Divers": ("Miscellaneous", "Verschiedenes", "Varios", "Varie"),
}
LANGS = ("en", "de", "es", "it")


def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").strip().lower()).encode("ascii", "ignore").decode()
    return " ".join(s.replace("&", " et ").replace("-", " ").split())


_INDEX = {_norm(fr): (fr, tr) for fr, tr in CATEGORIES.items()}


def known(category):
    return _norm(category) in _INDEX


def label(category, lang="fr"):
    """Nom de la categorie dans la langue du visiteur (inconnue : telle quelle)."""
    hit = _INDEX.get(_norm(category))
    lang = (lang or "fr")[:2]
    if not hit or lang not in LANGS:
        return category
    return hit[1][LANGS.index(lang)]


def names():
    """Noms francais proposes dans le catalogue (liste de suggestions)."""
    return list(CATEGORIES)
