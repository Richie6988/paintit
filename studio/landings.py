"""Pages thematiques SEO : le vocabulaire officiel de la peinture par numeros dans chaque langue
(peinture par numeros personnalisee / adulte / enfant / animal / cadeau / guide ; paint by numbers ;
Malen nach Zahlen ; pintar por numeros ; dipingere con i numeri).

Chaque page : slug propre a la langue, title / description / H1 uniques, contenu reel (pas de remplissage),
FAQ (donnees structurees), liens internes vers les autres pages et vers la creation.
Les chiffres (prix, formats, delais, couleurs) viennent des tarifs en vigueur : {price}, {formats}, {days}, {colors}.
"""

TOPICS = ["custom", "adult", "kids", "pet", "gift", "guide"]

PAGES = {
    # ------------------------------------------------------------------ FRANÇAIS
    "fr": {
        "custom": {
            "slug": "peinture-par-numeros-personnalisee",
            "title": "Peinture par numéros personnalisée avec votre photo",
            "desc": "Créez votre peinture par numéros personnalisée à partir de n'importe quelle photo. Aperçu gratuit, jeu en ligne, kit complet livré gratuitement dès {price} €.",
            "h1": "Peinture par numéros personnalisée à partir de votre photo",
            "lead": "Une photo de famille, un paysage de vacances, votre animal : PaintIt la transforme en toile numérotée à peindre chez vous, avec les peintures déjà dosées.",
            "sections": [
                ("Comment ça marche", [
                    "Envoyez votre photo : notre algorithme la découpe en zones et calcule la palette de couleurs la plus fidèle. En moins d'une minute, vous voyez le rendu final et la toile numérotée.",
                    "Vous pouvez tester votre toile gratuitement en ligne, comme un jeu, avant de commander. Rien n'est facturé tant que le résultat ne vous plaît pas.",
                    "Une fois validée, la toile est imprimée et livrée gratuitement en {days} jours ouvrés avec tout le nécessaire.",
                ]),
                ("Ce que contient le kit", [
                    "Une toile pré-imprimée avec les contours et les numéros, des peintures acryliques numérotées (une couleur par numéro) et, en option, un set de pinceaux.",
                    "Formats disponibles : {formats}. Nombre de couleurs : {colors}. Plus il y a de couleurs, plus le rendu est proche de la photo.",
                ]),
                ("Quelle photo choisir ?", [
                    "Une photo nette et bien éclairée, avec un sujet principal clairement visible. Les visages donnent les meilleurs résultats en niveau Expert, qui découpe des zones plus fines.",
                    "Évitez les photos floues, très sombres ou à contre-jour. Vous pouvez recadrer et ajuster le nombre de couleurs avant de commander.",
                ]),
            ],
            "faq": [
                ("Combien coûte une peinture par numéros personnalisée ?", "À partir de {price} €, livraison offerte. Le prix dépend du format et du nombre de couleurs."),
                ("Puis-je voir le résultat avant d'acheter ?", "Oui. L'aperçu et la toile numérotée sont générés gratuitement, et vous pouvez même la peindre en ligne avant de commander."),
                ("Faut-il savoir dessiner ?", "Non. Chaque zone porte un numéro qui correspond à un pot de peinture : il suffit de remplir les zones."),
            ],
        },
        "adult": {
            "slug": "peinture-par-numeros-adulte",
            "title": "Peinture par numéros adulte : kit à partir de votre photo",
            "desc": "Peinture par numéros pour adulte, détaillée et relaxante, créée à partir de votre photo. 3 niveaux de détail, jusqu'à 48 couleurs, aperçu gratuit.",
            "h1": "Peinture par numéros pour adulte",
            "lead": "Une activité créative et apaisante : quelques heures de concentration, un vrai tableau au mur à la fin. Et cette fois, c'est votre photo.",
            "sections": [
                ("Un niveau de détail adapté à vous", [
                    "Trois densités de zones : Facile pour une première toile, Moyen pour un bon équilibre, Expert pour un rendu très fin, idéal pour les portraits.",
                    "Jusqu'à {colors_max} couleurs pour des dégradés subtils. Le niveau de difficulté est indiqué avant l'achat.",
                ]),
                ("Un moment pour soi", [
                    "La peinture par numéros est souvent choisie pour décrocher des écrans : on avance zone par zone, sans pression, et le tableau apparaît peu à peu.",
                    "Comptez de 10 à 40 heures selon le format et le niveau. La peinture acrylique sèche vite, vous pouvez vous arrêter à tout moment.",
                ]),
                ("Un résultat digne d'être encadré", [
                    "Toile de qualité, peintures couvrantes, formats {formats}. Une fois terminée, la toile peut être encadrée ou montée sur châssis.",
                ]),
            ],
            "faq": [
                ("Combien de temps faut-il pour terminer ?", "En moyenne 10 à 40 heures, selon le format et le niveau de détail choisi."),
                ("Quel niveau choisir pour un portrait ?", "Le niveau Expert : ses zones plus fines restituent mieux les traits du visage."),
                ("Les peintures suffisent-elles ?", "Oui, chaque couleur est dosée pour la surface à peindre sur votre toile."),
            ],
        },
        "kids": {
            "slug": "peinture-par-numeros-enfant",
            "title": "Peinture par numéros enfant, dès 4 ans, avec sa photo",
            "desc": "Peinture par numéros pour enfant dès 4 ans : grandes zones, peu de couleurs, à partir d'une photo qu'il aime. Testez gratuitement en ligne.",
            "h1": "Peinture par numéros pour enfant",
            "lead": "Peindre son chien, sa maison ou un souvenir de vacances : la peinture par numéros apprend la patience et la précision, avec une vraie fierté à la fin.",
            "sections": [
                ("Simple dès 4 ans", [
                    "Choisissez le niveau Facile et 12 ou 24 couleurs : les zones sont grandes et faciles à remplir.",
                    "Avant de commander, l'enfant peut essayer gratuitement le jeu de peinture en ligne pour comprendre le principe.",
                ]),
                ("Une activité à partager", [
                    "Parents et enfants peuvent peindre ensemble, chacun sa partie de la toile. Les peintures acryliques se nettoient à l'eau tant qu'elles sont fraîches.",
                ]),
            ],
            "faq": [
                ("À partir de quel âge ?", "Dès 4 ans avec le niveau Facile et un accompagnement, en autonomie vers 7 ou 8 ans."),
                ("Les peintures sont-elles adaptées aux enfants ?", "Ce sont des peintures acryliques à l'eau. Comme pour toute peinture, une surveillance est conseillée pour les plus jeunes."),
                ("Quel format choisir ?", "Un petit format ({format_min}) pour une première toile, terminée en quelques séances."),
            ],
        },
        "pet": {
            "slug": "peinture-par-numeros-animal",
            "title": "Peinture par numéros de votre animal (chien, chat…)",
            "desc": "Transformez la photo de votre chien, chat ou cheval en peinture par numéros personnalisée. Aperçu gratuit, kit livré gratuitement dès {price} €.",
            "h1": "Peinture par numéros de votre animal",
            "lead": "Votre chien, votre chat, votre cheval : faites de leur photo préférée un tableau peint de vos mains.",
            "sections": [
                ("Les bonnes photos d'animaux", [
                    "Une photo prise à hauteur de l'animal, avec les yeux bien nets, donne le meilleur résultat. Recadrez au plus près pour que l'animal occupe la toile.",
                    "Le pelage gagne à être peint en 36 ou 48 couleurs et en niveau Moyen ou Expert.",
                ]),
                ("Un hommage durable", [
                    "Beaucoup de nos clients peignent un compagnon disparu : un tableau personnel, bien plus qu'une photo encadrée.",
                ]),
            ],
            "faq": [
                ("Le résultat est-il ressemblant ?", "Vous le voyez avant d'acheter : l'aperçu montre exactement la toile que vous recevrez."),
                ("Combien de couleurs pour un pelage ?", "36 ou 48 couleurs pour les nuances du pelage, 24 suffisent pour un animal au poil uni."),
            ],
        },
        "gift": {
            "slug": "cadeau-peinture-par-numeros",
            "title": "Cadeau peinture par numéros personnalisé",
            "desc": "Un cadeau original et personnel : une peinture par numéros créée à partir d'une photo souvenir. Fête des mères, Noël, anniversaire. Livraison offerte.",
            "h1": "Offrez une peinture par numéros personnalisée",
            "lead": "Un souvenir commun qui devient un tableau à peindre : un cadeau qu'on ne trouve dans aucun magasin.",
            "sections": [
                ("Pour quelles occasions ?", [
                    "Fête des mères et des pères, anniversaire, Noël, départ à la retraite, naissance : une photo de famille ou de moments partagés touche toujours juste.",
                    "Pour les grands-parents, un portrait des petits-enfants est un des cadeaux les plus appréciés.",
                ]),
                ("Commandez à temps", [
                    "Livraison offerte en {days} jours ouvrés. Pour une fête à date fixe, commandez au moins deux semaines avant.",
                    "Vous pouvez faire livrer directement chez la personne : indiquez simplement son adresse à la commande.",
                ]),
            ],
            "faq": [
                ("Puis-je livrer directement le destinataire ?", "Oui, saisissez son adresse de livraison lors de la commande."),
                ("Quel est le délai ?", "Comptez {days} jours ouvrés, livraison offerte."),
            ],
        },
        "guide": {
            "slug": "comment-faire-une-peinture-par-numeros",
            "title": "Comment faire une peinture par numéros : guide et astuces",
            "desc": "Guide complet de la peinture par numéros : matériel, ordre des couleurs, techniques, erreurs à éviter et finitions. Conseils pour un résultat net.",
            "h1": "Comment faire une peinture par numéros",
            "lead": "Le principe est simple, quelques gestes font la différence entre un tableau correct et un très beau tableau.",
            "sections": [
                ("Le matériel", [
                    "Une toile pré-imprimée avec les zones et leurs numéros, des pots de peinture acrylique numérotés, deux ou trois pinceaux (un fin, un moyen), un verre d'eau et un chiffon.",
                ]),
                ("Les étapes", [
                    "1. Installez-vous à la lumière du jour, toile bien à plat. 2. Ouvrez une seule couleur à la fois et refermez le pot après usage. 3. Peignez couleur par couleur en commençant par les plus foncées, ou du haut vers le bas pour ne pas poser la main sur la peinture fraîche. 4. Couvrez bien les numéros et les contours : deux couches fines valent mieux qu'une épaisse.",
                ]),
                ("Astuces de pro", [
                    "Utilisez le pinceau fin pour les petites zones et les bords. Rincez le pinceau entre chaque couleur. Si une peinture a épaissi, ajoutez une goutte d'eau.",
                    "Une erreur ? Laissez sécher puis repeignez par-dessus avec la bonne couleur : l'acrylique est couvrante.",
                ]),
                ("Finitions", [
                    "Une fois sèche (24 h), la toile peut recevoir un vernis acrylique pour protéger les couleurs, puis être encadrée ou montée sur châssis.",
                ]),
            ],
            "faq": [
                ("Par quelle couleur commencer ?", "Par les plus foncées, ou du haut vers le bas : les erreurs se corrigent plus facilement."),
                ("Comment ne plus voir les numéros ?", "Passez une deuxième couche fine une fois la première sèche."),
                ("Faut-il vernir ?", "Ce n'est pas obligatoire, mais un vernis acrylique protège la toile et ravive les couleurs."),
            ],
        },
    },
    # ------------------------------------------------------------------ ENGLISH
    "en": {
        "custom": {
            "slug": "custom-paint-by-numbers",
            "title": "Custom paint by numbers from your photo",
            "desc": "Turn any photo into a custom paint-by-numbers kit. Free preview and online game, complete kit with free shipping from €{price}.",
            "h1": "Custom paint by numbers from your photo",
            "lead": "A family picture, a holiday view, your pet: PaintIt turns it into a numbered canvas to paint at home, with the paints already matched.",
            "sections": [
                ("How it works", [
                    "Upload your photo: our algorithm splits it into areas and computes the most faithful colour palette. In under a minute you see the final result and the numbered canvas.",
                    "You can try your canvas online for free, like a game, before ordering. Nothing is charged until you love the result.",
                    "Once approved, the canvas is printed and shipped free of charge in {days} working days with everything you need.",
                ]),
                ("What's in the kit", [
                    "A pre-printed canvas with outlines and numbers, numbered acrylic paints (one colour per number) and an optional brush set.",
                    "Sizes: {formats}. Number of colours: {colors}. More colours means a result closer to your photo.",
                ]),
                ("Which photo works best?", [
                    "A sharp, well-lit photo with a clear main subject. Faces look best with the Expert level, which uses finer areas.",
                ]),
            ],
            "faq": [
                ("How much does a custom paint by numbers cost?", "From €{price} with free shipping. The price depends on the size and number of colours."),
                ("Can I see the result before buying?", "Yes. The preview and numbered canvas are free, and you can even paint it online before ordering."),
                ("Do I need to know how to draw?", "No. Each area has a number matching a paint pot: you just fill in the areas."),
            ],
        },
        "adult": {
            "slug": "paint-by-numbers-for-adults",
            "title": "Paint by numbers for adults, from your own photo",
            "desc": "Detailed, relaxing paint by numbers for adults made from your photo. Three detail levels, up to 48 colours, free preview.",
            "h1": "Paint by numbers for adults",
            "lead": "A calming creative hobby: a few hours of focus and a real painting on your wall. This time, it's your photo.",
            "sections": [
                ("Detail that suits you", [
                    "Three area densities: Easy for a first canvas, Medium for balance, Expert for very fine detail, ideal for portraits. Up to {colors_max} colours for subtle shading.",
                ]),
                ("Time for yourself", [
                    "Paint by numbers is a popular way to step away from screens: area by area, no pressure, the picture slowly appears. Expect 10 to 40 hours depending on size and level.",
                ]),
            ],
            "faq": [
                ("How long does it take?", "Usually 10 to 40 hours, depending on the size and detail level."),
                ("Which level for a portrait?", "Expert: its finer areas capture facial features best."),
            ],
        },
        "kids": {
            "slug": "paint-by-numbers-for-kids",
            "title": "Paint by numbers for kids, from age 4",
            "desc": "Paint by numbers for kids from age 4: big areas, few colours, made from a photo they love. Try it free online.",
            "h1": "Paint by numbers for kids",
            "lead": "Painting their dog, their house or a holiday memory: paint by numbers builds patience and precision, with real pride at the end.",
            "sections": [
                ("Easy from age 4", [
                    "Pick the Easy level and 12 or 24 colours: areas are large and simple to fill. Kids can try the free online painting game first to understand how it works.",
                ]),
                ("A shared activity", [
                    "Parents and children can paint together, each on a part of the canvas. Acrylic paints wash off with water while wet.",
                ]),
            ],
            "faq": [
                ("From what age?", "From 4 with the Easy level and some help, independently from about 7 or 8."),
                ("Which size?", "A small size ({format_min}) for a first canvas, finished in a few sessions."),
            ],
        },
        "pet": {
            "slug": "pet-paint-by-numbers",
            "title": "Pet paint by numbers: your dog or cat on canvas",
            "desc": "Turn a photo of your dog, cat or horse into a custom paint by numbers. Free preview, kit shipped free from €{price}.",
            "h1": "Paint by numbers of your pet",
            "lead": "Your dog, cat or horse: make their best photo a painting by your own hand.",
            "sections": [
                ("Great pet photos", [
                    "A photo taken at the animal's eye level, with sharp eyes, gives the best result. Crop close so your pet fills the canvas. Fur looks best with 36 or 48 colours.",
                ]),
                ("A lasting tribute", [
                    "Many customers paint a pet they lost: a personal painting, far more than a framed photo.",
                ]),
            ],
            "faq": [
                ("Will it look like my pet?", "You see it before buying: the preview shows exactly the canvas you will receive."),
            ],
        },
        "gift": {
            "slug": "paint-by-numbers-gift",
            "title": "Personalised paint by numbers gift",
            "desc": "An original, personal gift: paint by numbers made from a treasured photo. Mother's Day, Christmas, birthdays. Free shipping.",
            "h1": "Give a personalised paint by numbers",
            "lead": "A shared memory turned into a painting to make: a gift you won't find in any shop.",
            "sections": [
                ("For every occasion", [
                    "Mother's and Father's Day, birthdays, Christmas, retirement, a new baby: a family photo always hits the mark.",
                ]),
                ("Order in time", [
                    "Free shipping in {days} working days. For a fixed date, order at least two weeks ahead. You can ship straight to the recipient.",
                ]),
            ],
            "faq": [
                ("Can you ship to the recipient?", "Yes, just enter their delivery address when ordering."),
            ],
        },
        "guide": {
            "slug": "how-to-paint-by-numbers",
            "title": "How to paint by numbers: complete guide and tips",
            "desc": "Complete paint by numbers guide: materials, colour order, techniques, mistakes to avoid and finishing. Tips for a clean result.",
            "h1": "How to paint by numbers",
            "lead": "The principle is simple; a few habits make the difference between a decent painting and a beautiful one.",
            "sections": [
                ("Materials", [
                    "A pre-printed canvas with numbered areas, numbered acrylic paint pots, two or three brushes (fine and medium), a glass of water and a cloth.",
                ]),
                ("Steps", [
                    "1. Work in daylight with the canvas flat. 2. Open one colour at a time and close it after use. 3. Paint colour by colour, starting with the darkest, or top to bottom so your hand never rests on wet paint. 4. Cover numbers and outlines fully: two thin coats beat one thick coat.",
                ]),
                ("Pro tips", [
                    "Use the fine brush for small areas and edges, rinse between colours, add a drop of water to thickened paint. Made a mistake? Let it dry and paint over it: acrylic covers well.",
                ]),
            ],
            "faq": [
                ("Which colour first?", "The darkest, or work top to bottom: mistakes are easier to fix."),
                ("How do I hide the numbers?", "Apply a second thin coat once the first is dry."),
            ],
        },
    },
    # ------------------------------------------------------------------ DEUTSCH
    "de": {
        "custom": {
            "slug": "malen-nach-zahlen-eigenes-foto",
            "title": "Malen nach Zahlen mit eigenem Foto",
            "desc": "Malen nach Zahlen nach eigenem Foto: kostenlose Vorschau, Online-Spiel, komplettes Set mit kostenlosem Versand ab {price} €.",
            "h1": "Malen nach Zahlen nach eigenem Foto",
            "lead": "Familienfoto, Urlaubsmotiv oder Haustier: PaintIt macht daraus eine nummerierte Leinwand zum Ausmalen, mit passend gemischten Farben.",
            "sections": [
                ("So funktioniert's", [
                    "Foto hochladen: Unser Algorithmus teilt es in Felder und berechnet die passende Farbpalette. In weniger als einer Minute sehen Sie das Ergebnis. Testen Sie Ihre Leinwand kostenlos online, bevor Sie bestellen.",
                ]),
                ("Inhalt des Sets", [
                    "Vorgedruckte Leinwand mit Konturen und Zahlen, nummerierte Acrylfarben und optional ein Pinselset. Formate: {formats}. Farben: {colors}. Kostenloser Versand in {days} Werktagen.",
                ]),
            ],
            "faq": [
                ("Was kostet Malen nach Zahlen mit eigenem Foto?", "Ab {price} €, Versand kostenlos."),
                ("Kann ich das Ergebnis vorher sehen?", "Ja, Vorschau und nummerierte Leinwand sind kostenlos."),
            ],
        },
        "adult": {
            "slug": "malen-nach-zahlen-erwachsene",
            "title": "Malen nach Zahlen für Erwachsene, nach eigenem Foto",
            "desc": "Detailreiches Malen nach Zahlen für Erwachsene nach Ihrem Foto: drei Schwierigkeitsstufen, bis zu 48 Farben, kostenlose Vorschau.",
            "h1": "Malen nach Zahlen für Erwachsene",
            "lead": "Ein entspannendes Hobby mit echtem Ergebnis an der Wand – diesmal mit Ihrem eigenen Foto.",
            "sections": [
                ("Der passende Detailgrad", [
                    "Drei Stufen: Leicht, Mittel und Experte (ideal für Porträts). Bis zu {colors_max} Farben für feine Übergänge. Rechnen Sie mit 10 bis 40 Stunden.",
                ]),
            ],
            "faq": [("Welche Stufe für ein Porträt?", "Experte: die feineren Felder geben Gesichtszüge am besten wieder.")],
        },
        "kids": {
            "slug": "malen-nach-zahlen-kinder",
            "title": "Malen nach Zahlen für Kinder ab 4 Jahren",
            "desc": "Malen nach Zahlen für Kinder ab 4: große Felder, wenige Farben, nach einem Lieblingsfoto. Kostenlos online ausprobieren.",
            "h1": "Malen nach Zahlen für Kinder",
            "lead": "Den eigenen Hund oder ein Urlaubsfoto malen: fördert Geduld und Genauigkeit.",
            "sections": [
                ("Einfach ab 4 Jahren", ["Stufe Leicht mit 12 oder 24 Farben wählen: große, einfache Felder. Vorher kostenlos das Online-Malspiel testen."]),
            ],
            "faq": [("Ab welchem Alter?", "Ab 4 Jahren mit Begleitung, selbstständig ab etwa 7 bis 8 Jahren.")],
        },
        "pet": {
            "slug": "malen-nach-zahlen-haustier",
            "title": "Malen nach Zahlen: Ihr Haustier auf Leinwand",
            "desc": "Hund, Katze oder Pferd als Malen nach Zahlen nach Ihrem Foto. Kostenlose Vorschau, Versand kostenlos ab {price} €.",
            "h1": "Malen nach Zahlen von Ihrem Haustier",
            "lead": "Machen Sie das schönste Foto Ihres Tieres zu einem selbst gemalten Bild.",
            "sections": [("Gute Tierfotos", ["Auf Augenhöhe fotografiert, mit scharfen Augen. Für Fell 36 oder 48 Farben wählen."])],
            "faq": [("Wird es meinem Tier ähnlich sehen?", "Sie sehen es vor dem Kauf: die Vorschau zeigt genau Ihre Leinwand.")],
        },
        "gift": {
            "slug": "malen-nach-zahlen-geschenk",
            "title": "Malen nach Zahlen als persönliches Geschenk",
            "desc": "Ein persönliches Geschenk: Malen nach Zahlen nach einem Erinnerungsfoto. Muttertag, Weihnachten, Geburtstag. Kostenloser Versand.",
            "h1": "Malen nach Zahlen verschenken",
            "lead": "Eine gemeinsame Erinnerung als Bild zum Selbermalen – ein Geschenk, das es in keinem Laden gibt.",
            "sections": [("Rechtzeitig bestellen", ["Kostenloser Versand in {days} Werktagen, auch direkt an die beschenkte Person."])],
            "faq": [("Kann direkt an den Empfänger geliefert werden?", "Ja, geben Sie einfach dessen Adresse an.")],
        },
        "guide": {
            "slug": "malen-nach-zahlen-anleitung",
            "title": "Malen nach Zahlen: Anleitung und Tipps",
            "desc": "Anleitung Malen nach Zahlen: Material, Reihenfolge der Farben, Techniken und Fehler vermeiden.",
            "h1": "Anleitung: Malen nach Zahlen",
            "lead": "Einfaches Prinzip – mit ein paar Tricks wird das Bild deutlich schöner.",
            "sections": [("Schritte", ["Bei Tageslicht arbeiten, immer nur eine Farbe öffnen, mit den dunkelsten Farben oder von oben nach unten beginnen, Zahlen und Konturen vollständig abdecken: lieber zwei dünne Schichten."])],
            "faq": [("Wie verschwinden die Zahlen?", "Mit einer zweiten dünnen Schicht nach dem Trocknen.")],
        },
    },
    # ------------------------------------------------------------------ ESPAÑOL
    "es": {
        "custom": {
            "slug": "pintar-por-numeros-personalizado",
            "title": "Pintar por números personalizado con tu foto",
            "desc": "Cuadro para pintar por números personalizado a partir de tu foto. Vista previa gratis, juego online, kit completo con envío gratis desde {price} €.",
            "h1": "Pintar por números personalizado con tu foto",
            "lead": "Una foto de familia, un paisaje, tu mascota: PaintIt la convierte en un lienzo numerado para pintar en casa, con las pinturas ya preparadas.",
            "sections": [
                ("Cómo funciona", ["Sube tu foto: nuestro algoritmo la divide en zonas y calcula la paleta más fiel. En menos de un minuto ves el resultado y puedes probar tu lienzo gratis online antes de comprar."]),
                ("Qué incluye el kit", ["Lienzo preimpreso con contornos y números, pinturas acrílicas numeradas y, opcionalmente, pinceles. Formatos: {formats}. Colores: {colors}. Envío gratis en {days} días laborables."]),
            ],
            "faq": [("¿Cuánto cuesta?", "Desde {price} €, envío gratis."), ("¿Puedo ver el resultado antes?", "Sí, la vista previa es gratuita.")],
        },
        "adult": {
            "slug": "pintar-por-numeros-adultos",
            "title": "Pintar por números para adultos con tu propia foto",
            "desc": "Pintar por números para adultos, detallado y relajante, a partir de tu foto. Tres niveles, hasta 48 colores, vista previa gratis.",
            "h1": "Pintar por números para adultos",
            "lead": "Una afición relajante con un cuadro real al final, esta vez con tu foto.",
            "sections": [("El nivel adecuado", ["Tres niveles: Fácil, Medio y Experto (ideal para retratos). Hasta {colors_max} colores. Entre 10 y 40 horas según formato."])],
            "faq": [("¿Qué nivel para un retrato?", "Experto: sus zonas más finas reproducen mejor el rostro.")],
        },
        "kids": {
            "slug": "pintar-por-numeros-ninos",
            "title": "Pintar por números para niños desde 4 años",
            "desc": "Pintar por números para niños desde 4 años: zonas grandes, pocos colores, con su foto favorita. Pruébalo gratis online.",
            "h1": "Pintar por números para niños",
            "lead": "Pintar su perro o un recuerdo de vacaciones desarrolla la paciencia y la precisión.",
            "sections": [("Fácil desde 4 años", ["Nivel Fácil con 12 o 24 colores: zonas grandes y sencillas. Antes, prueba gratis el juego online."])],
            "faq": [("¿Desde qué edad?", "Desde 4 años con ayuda, solos hacia los 7 u 8 años.")],
        },
        "pet": {
            "slug": "pintar-por-numeros-mascota",
            "title": "Pintar por números de tu mascota",
            "desc": "Convierte la foto de tu perro, gato o caballo en un cuadro para pintar por números. Vista previa gratis, envío gratis desde {price} €.",
            "h1": "Pintar por números de tu mascota",
            "lead": "Haz de la mejor foto de tu mascota un cuadro pintado por ti.",
            "sections": [("Buenas fotos", ["Foto a la altura del animal, con los ojos nítidos. Para el pelaje, 36 o 48 colores."])],
            "faq": [("¿Se parecerá a mi mascota?", "Lo ves antes de comprar: la vista previa muestra tu lienzo exacto.")],
        },
        "gift": {
            "slug": "regalo-pintar-por-numeros",
            "title": "Regalo original: pintar por números personalizado",
            "desc": "Un regalo original y personal: pintar por números a partir de una foto recuerdo. Día de la Madre, Navidad, cumpleaños. Envío gratis.",
            "h1": "Regala un cuadro para pintar por números",
            "lead": "Un recuerdo compartido convertido en un cuadro para pintar: un regalo que no está en ninguna tienda.",
            "sections": [("Pide a tiempo", ["Envío gratis en {days} días laborables, también directamente al destinatario."])],
            "faq": [("¿Podéis enviarlo al destinatario?", "Sí, indica su dirección al hacer el pedido.")],
        },
        "guide": {
            "slug": "como-pintar-por-numeros",
            "title": "Cómo pintar por números: guía y consejos",
            "desc": "Guía para pintar por números: material, orden de colores, técnicas y errores que evitar.",
            "h1": "Cómo pintar por números",
            "lead": "El principio es simple; algunos trucos marcan la diferencia.",
            "sections": [("Pasos", ["Trabaja con luz natural, abre un solo color cada vez, empieza por los más oscuros o de arriba abajo y cubre bien números y contornos: mejor dos capas finas."])],
            "faq": [("¿Cómo ocultar los números?", "Con una segunda capa fina cuando la primera esté seca.")],
        },
    },
    # ------------------------------------------------------------------ ITALIANO
    "it": {
        "custom": {
            "slug": "dipingere-con-i-numeri-personalizzato",
            "title": "Dipingere con i numeri personalizzato con la tua foto",
            "desc": "Quadro da dipingere con i numeri personalizzato dalla tua foto. Anteprima gratuita, gioco online, kit completo con spedizione gratuita da {price} €.",
            "h1": "Dipingere con i numeri personalizzato dalla tua foto",
            "lead": "Una foto di famiglia, un paesaggio, il tuo animale: PaintIt la trasforma in una tela numerata da dipingere a casa, con i colori già pronti.",
            "sections": [
                ("Come funziona", ["Carica la foto: il nostro algoritmo la divide in zone e calcola la palette più fedele. In meno di un minuto vedi il risultato e puoi provare la tela gratis online prima di ordinare."]),
                ("Cosa contiene il kit", ["Tela prestampata con contorni e numeri, colori acrilici numerati e, a scelta, pennelli. Formati: {formats}. Colori: {colors}. Spedizione gratuita in {days} giorni lavorativi."]),
            ],
            "faq": [("Quanto costa?", "Da {price} €, spedizione gratuita."), ("Posso vedere il risultato prima?", "Sì, l'anteprima è gratuita.")],
        },
        "adult": {
            "slug": "dipingere-con-i-numeri-adulti",
            "title": "Dipingere con i numeri per adulti dalla tua foto",
            "desc": "Dipingere con i numeri per adulti, dettagliato e rilassante, dalla tua foto. Tre livelli, fino a 48 colori, anteprima gratuita.",
            "h1": "Dipingere con i numeri per adulti",
            "lead": "Un hobby rilassante con un vero quadro alla fine, questa volta con la tua foto.",
            "sections": [("Il livello giusto", ["Tre livelli: Facile, Medio ed Esperto (ideale per i ritratti). Fino a {colors_max} colori. Da 10 a 40 ore secondo il formato."])],
            "faq": [("Quale livello per un ritratto?", "Esperto: le zone più fini rendono meglio i lineamenti.")],
        },
        "kids": {
            "slug": "dipingere-con-i-numeri-bambini",
            "title": "Dipingere con i numeri per bambini dai 4 anni",
            "desc": "Dipingere con i numeri per bambini dai 4 anni: zone grandi, pochi colori, dalla loro foto preferita. Prova gratis online.",
            "h1": "Dipingere con i numeri per bambini",
            "lead": "Dipingere il proprio cane o un ricordo delle vacanze sviluppa pazienza e precisione.",
            "sections": [("Facile dai 4 anni", ["Livello Facile con 12 o 24 colori: zone grandi e semplici. Prima, prova gratis il gioco online."])],
            "faq": [("Da che età?", "Dai 4 anni con un adulto, da soli verso i 7-8 anni.")],
        },
        "pet": {
            "slug": "dipingere-con-i-numeri-animali",
            "title": "Dipingere con i numeri il tuo animale",
            "desc": "Trasforma la foto del tuo cane, gatto o cavallo in un quadro da dipingere con i numeri. Anteprima gratuita, spedizione gratuita da {price} €.",
            "h1": "Dipingere con i numeri il tuo animale",
            "lead": "Fai della foto più bella del tuo animale un quadro dipinto da te.",
            "sections": [("Buone foto", ["Scatta all'altezza dell'animale, con gli occhi nitidi. Per il pelo, 36 o 48 colori."])],
            "faq": [("Somiglierà al mio animale?", "Lo vedi prima di acquistare: l'anteprima mostra la tua tela esatta.")],
        },
        "gift": {
            "slug": "regalo-dipingere-con-i-numeri",
            "title": "Regalo originale: dipingere con i numeri personalizzato",
            "desc": "Un regalo originale e personale: dipingere con i numeri da una foto ricordo. Festa della mamma, Natale, compleanni. Spedizione gratuita.",
            "h1": "Regala un quadro da dipingere con i numeri",
            "lead": "Un ricordo condiviso diventa un quadro da dipingere: un regalo che non si trova in nessun negozio.",
            "sections": [("Ordina in tempo", ["Spedizione gratuita in {days} giorni lavorativi, anche direttamente al destinatario."])],
            "faq": [("Potete spedire al destinatario?", "Sì, inserisci il suo indirizzo all'ordine.")],
        },
        "guide": {
            "slug": "come-dipingere-con-i-numeri",
            "title": "Come dipingere con i numeri: guida e consigli",
            "desc": "Guida per dipingere con i numeri: materiale, ordine dei colori, tecniche ed errori da evitare.",
            "h1": "Come dipingere con i numeri",
            "lead": "Il principio è semplice; qualche trucco fa la differenza.",
            "sections": [("Passaggi", ["Lavora con luce naturale, apri un colore alla volta, inizia dai più scuri o dall'alto verso il basso e copri bene numeri e contorni: meglio due mani sottili."])],
            "faq": [("Come nascondere i numeri?", "Con una seconda mano sottile quando la prima è asciutta.")],
        },
    },
}

NAV_LABEL = {"fr": "Peinture par numéros", "en": "Paint by numbers", "de": "Malen nach Zahlen",
             "es": "Pintar por números", "it": "Dipingere con i numeri"}
SHORT = {  # libelles courts (pied de page / liens)
    "fr": {"custom": "Personnalisée", "adult": "Adulte", "kids": "Enfant", "pet": "Animaux", "gift": "Idée cadeau", "guide": "Guide et astuces"},
    "en": {"custom": "Custom", "adult": "For adults", "kids": "For kids", "pet": "Pets", "gift": "Gift idea", "guide": "How-to guide"},
    "de": {"custom": "Eigenes Foto", "adult": "Erwachsene", "kids": "Kinder", "pet": "Haustiere", "gift": "Geschenk", "guide": "Anleitung"},
    "es": {"custom": "Personalizado", "adult": "Adultos", "kids": "Niños", "pet": "Mascotas", "gift": "Regalo", "guide": "Guía"},
    "it": {"custom": "Personalizzato", "adult": "Adulti", "kids": "Bambini", "pet": "Animali", "gift": "Regalo", "guide": "Guida"},
}


def lang_prefix(lang):
    from django.conf import settings
    return "" if lang == settings.LANGUAGE_CODE else "/" + lang


def url(lang, topic):
    p = PAGES.get(lang, {}).get(topic)
    return "%s/%s/" % (lang_prefix(lang), p["slug"]) if p else None


def find(slug):
    """-> (lang, topic) pour un slug, toutes langues confondues."""
    for lang, pages in PAGES.items():
        for topic, p in pages.items():
            if p["slug"] == slug:
                return lang, topic
    return None, None


def facts(lang="fr"):
    """Chiffres reels (tarifs en vigueur) injectes dans les textes."""
    from .models import KitFormat, Pricing, COLOR_COUNTS
    p = Pricing.get()
    kfs = [k for k in KitFormat.objects.all() if k.available]
    cols = [n for n in COLOR_COUNTS if getattr(p, "av_c%d" % n, True)]
    base = min((float(k.price) for k in kfs), default=0.0) + (p.color_price(min(cols)) if cols else 0)
    labels = ["%d×%d cm" % (k.width_cm, k.height_cm) for k in sorted(kfs, key=lambda k: k.width_cm * k.height_cm)]
    price = ("%.2f" % base) if base else ""
    if price.endswith(".00"):
        price = price[:-3]
    if lang != "en":
        price = price.replace(".", ",")
    return {"price": price,
            "formats": ", ".join(labels) or "30×40 à 50×70 cm",
            "format_min": labels[0] if labels else "30×40 cm",
            "colors": ", ".join(str(n) for n in cols) or "24, 36, 48",
            "colors_max": max(cols) if cols else 48,
            "days": "%s-%s" % (p.delivery_days_min, p.delivery_days_max)}


def render_page(lang, topic):
    """Page prete a afficher (textes avec chiffres reels) + liens vers les autres pages de la langue."""
    p = PAGES[lang][topic]
    f = facts(lang)
    fmt = lambda s: s.format(**f)
    return {"topic": topic, "lang": lang, "slug": p["slug"], "title": fmt(p["title"]), "desc": fmt(p["desc"]),
            "h1": fmt(p["h1"]), "lead": fmt(p["lead"]),
            "sections": [(h, [fmt(x) for x in paras]) for h, paras in p["sections"]],
            "faq": [(fmt(q), fmt(a)) for q, a in p["faq"]],
            "related": [(SHORT[lang][t], url(lang, t)) for t in TOPICS if t != topic and t in PAGES[lang]],
            "alternates": [(l, url(l, topic)) for l in PAGES if topic in PAGES[l]]}


def footer_links(lang):
    lang = lang if lang in PAGES else "fr"
    return {"label": NAV_LABEL[lang], "links": [(SHORT[lang][t], url(lang, t)) for t in TOPICS]}
