"""Bibliotheque de depart : accroches et publications multimedia pre-remplies (modifiables dans l'admin).

Principes : toujours la verite (les [CROCHETS] sont a remplacer par de vrais details), transparence
(« I'm the founder / je suis le fondateur ») des qu'on parle de PaintIt, {link} = lien traque unique par communaute.
Canaux : reddit, facebook, social (Instagram/TikTok/Pinterest/YouTube), launch (Product Hunt/HN), forum (Q&A), discord.
"""

DISCLOSE_EN = "Full disclosure: I'm the founder of PaintIt, the small tool I used to make it."
DISCLOSE_FR = "En toute transparence : je suis le fondateur de PaintIt, l'outil que j'ai utilisé."

LIBRARY = [
    # ------------------------------------------------------------------ HISTOIRE / EMOTION (EN)
    dict(name="Grand-parents — souvenir peint", category="story", lang="en", platforms="reddit,facebook",
         hook="I turned the only photo I have of my grandparents dancing into a paint by numbers",
         body="Took me [X] evenings. The original photo is from [YEAR] — 2nd picture.\n\nPainting their faces zone by zone "
              "felt like spending time with them again.\n\n" + DISCLOSE_EN + " Happy to answer questions about the process.",
         media_kind="beforeafter", media_brief="Photo 1 : toile peinte terminée, lumière naturelle, de face. Photo 2 : la photo "
         "d'origine (avec l'accord de la famille)."),
    dict(name="Chien disparu — hommage", category="story", lang="en", platforms="reddit,facebook,social",
         hook="We lost [DOG'S NAME] last spring. My daughter wanted to paint him, so we did it together",
         body="[AGE]-year-old me + [AGE]-year-old her, 24 colours, [X] weekends. She did the background, I did his eyes.\n\n"
              + DISCLOSE_EN, media_kind="photo",
         media_brief="Photo de la toile tenue par les mains de l'enfant (visage non visible), le chien bien centré."),
    dict(name="Premier tableau à 70 ans", category="story", lang="en", platforms="reddit,facebook",
         hook="My dad (72) had never painted in his life. This is his first canvas — from a photo of his old fishing boat",
         body="12 colours, big zones, he painted 20 minutes a day for [X] weeks. He's already asking for the next one.\n\n"
              + DISCLOSE_EN, media_kind="beforeafter",
         media_brief="La toile terminée posée sur une table, à côté des pots ouverts et du pinceau ; photo d'origine en 2e."),
    dict(name="Cadeau de mariage peint par les témoins", category="story", lang="en", platforms="reddit,facebook,social",
         hook="As best man I couldn't write a speech, so I painted their engagement photo instead",
         body="Gave it at the wedding, the bride cried, I count that as a win. 36 colours, [X] hours over [X] weeks.\n\n"
              + DISCLOSE_EN, media_kind="video",
         media_brief="Vidéo 10-15 s : la toile emballée, le déballage, la réaction (avec l'accord des mariés)."),
    dict(name="Maison d'enfance", category="story", lang="en", platforms="reddit,facebook",
         hook="Our childhood house was sold last year. I painted it so it stays on our wall",
         body="Photo from [YEAR], painted over [X] weeks. Every window has a memory.\n\n" + DISCLOSE_EN,
         media_kind="beforeafter", media_brief="Toile accrochée au mur dans la maison actuelle + photo d'origine."),
    # ------------------------------------------------------------------ AVANT / APRES
    dict(name="Photo → numéros → peint (triptyque)", category="beforeafter", lang="en", platforms="reddit,social",
         hook="Photo → numbered canvas → finished painting. Same picture, three stages",
         body="I'm always amazed at stage 2: it looks like abstract art until you add colour.\n\n" + DISCLOSE_EN +
              " You can try stage 1→2 with your own photo for free: {link}", media_kind="carousel",
         media_brief="Carrousel 3 images même cadrage : photo / toile numérotée vierge / toile peinte."),
    dict(name="Timelapse 30 h en 30 s", category="beforeafter", lang="en", platforms="social",
         hook="30 hours of painting in 30 seconds 🎨",
         body="Custom paint by numbers from a photo of [SUBJECT]. Which stage is your favourite? #paintbynumbers #custompaintbynumbers",
         media_kind="video", media_brief="Timelapse vertical 9:16, caméra fixe au-dessus, révélation finale avec la photo d'origine."),
    dict(name="Le zoom sur les petites zones", category="beforeafter", lang="en", platforms="reddit,social",
         hook="The tiniest zone on this canvas is [X] mm. Here's why that matters",
         body="Most custom kits I tried had zones smaller than a brush tip. We set a minimum paintable size so every area can "
              "actually be painted.\n\n" + DISCLOSE_EN, media_kind="photo",
         media_brief="Macro sur une zone de visage avec le pinceau à côté pour l'échelle."),
    dict(name="12 vs 24 vs 36 couleurs", category="beforeafter", lang="en", platforms="reddit,social",
         hook="Same photo in 12, 24 and 36 colours — which one would you paint?",
         body="12 = relaxing, great for kids and beginners. 36 = portrait-level detail. Curious what this sub prefers.\n\n"
              + DISCLOSE_EN, media_kind="carousel",
         media_brief="Carrousel : 3 aperçus colorés de la même photo (12/24/36), légende du nombre de couleurs sur chaque image."),
    dict(name="Recto numéroté / verso peint (vidéo split)", category="beforeafter", lang="en", platforms="social",
         hook="Slide to see what 2,000 numbers turn into",
         body="Swipe ➡️ Custom paint by numbers from your own photo. Try it free: link in bio.",
         media_kind="video", media_brief="Vidéo avec curseur avant/après qui glisse (enregistrement écran de l'aperçu PaintIt)."),
    # ------------------------------------------------------------------ CADEAU
    dict(name="Réponse : idée cadeau personnalisée", category="gift", lang="en", platforms="reddit,forum",
         hook="(reply) A gift they make themselves",
         body="If they like crafts, a paint-by-numbers made from a photo that matters to them (pet, wedding, family) is very "
              "personal: they paint it and keep it on the wall. " + DISCLOSE_EN + " You can preview it from your photo for "
              "free before buying anything: {link}", media_kind="text", media_brief=""),
    dict(name="Fête des mères — dernière minute", category="season", lang="en", platforms="social,facebook",
         hook="Mother's Day gift that takes 2 minutes to order and 20 hours to love",
         body="Her favourite photo → a canvas she paints herself. Free online preview first: {link}",
         media_kind="video", media_brief="Vidéo : mains d'une mère qui peint, puis la photo d'origine (famille, avec accord)."),
    dict(name="Cadeau couple — anniversaire", category="gift", lang="en", platforms="social,reddit",
         hook="Anniversary idea: paint your first photo together, together",
         body="A date night that lasts a month. We did ours in [X] evenings. " + DISCLOSE_EN + " {link}",
         media_kind="photo", media_brief="Deux mains qui peignent la même toile, verres de vin à côté."),
    dict(name="Noël — cadeau qui se finit en janvier", category="season", lang="en", platforms="social,facebook,reddit",
         hook="The Christmas gift that's still on the wall in July",
         body="Turn a family photo into a paint-by-numbers kit. Order before [DATE] for delivery before Christmas. {link}",
         media_kind="carousel", media_brief="Carrousel : sapin + kit emballé / déballage / toile finie accrochée."),
    dict(name="Cadeau de départ collègue", category="gift", lang="en", platforms="reddit,forum",
         hook="(reply) Leaving gift for a colleague",
         body="One idea: the team photo as a paint-by-numbers — they paint it at home and remember everyone. "
              + DISCLOSE_EN + " {link}", media_kind="text", media_brief=""),
    # ------------------------------------------------------------------ TECHNIQUE / LANCEMENT
    dict(name="Show HN — algorithme", category="tech", lang="en", platforms="launch",
         hook="Show HN: Photo to paint-by-numbers with paintable zones and real paint colours",
         body="Two problems with custom PBN kits: unpaintable micro-areas, and colours the factory can't reproduce. Pipeline: "
              "mean-shift smoothing → k-means in Lab → snap to the supplier's real paints (CIEDE2000) → zone merging with a "
              "minimum paintable size → shared-edge SVG so outlines never gap. Free to try in the browser: {link}",
         media_kind="gif", media_brief="GIF : upload d'une photo → génération → aperçu, en 6-8 s."),
    dict(name="r/SideProject — retours", category="launch", lang="en", platforms="reddit",
         hook="I built a tool that turns any photo into a paint-by-numbers canvas you can paint in the browser",
         body="Side project turned small business. The hardest part was keeping numbers readable and zones paintable.\n\n"
              "What I'd love feedback on: result quality on faces, and the onboarding. Try it (free, no sign-up): {link}",
         media_kind="gif", media_brief="GIF de la génération + 3 s du jeu en ligne."),
    dict(name="Product Hunt — lancement", category="launch", lang="en", platforms="launch",
         hook="PaintIt — Turn any photo into a paint-by-numbers canvas",
         body="Your memory, your masterpiece. Preview your canvas for free in seconds, paint it online, and get the full kit "
              "delivered.\n\nMaker comment: why we built it, what's different (paintable zones, real paint colours, free "
              "online game), and what we'd love your feedback on. {link}", media_kind="carousel",
         media_brief="5 visuels 1270x760 : avant/après, interface, jeu en ligne, kit livré, toile au mur."),
    dict(name="Couleurs réellement disponibles", category="tech", lang="en", platforms="reddit,launch",
         hook="Why your custom paint-by-numbers never matches the preview (and how we fixed it)",
         body="The preview uses millions of colours; the factory has a fixed set of paints. We snap every colour to the "
              "closest paint that actually exists (CIEDE2000), so what you see is what you paint.\n\n" + DISCLOSE_EN,
         media_kind="carousel", media_brief="Carrousel : aperçu « libre » vs aperçu « nuancier réel » + nuancier en pastilles."),
    dict(name="Jeu en ligne gratuit", category="launch", lang="en", platforms="reddit,social",
         hook="You can now paint your own photo as a paint-by-numbers in the browser, for free",
         body="No account, works on phone. If you like it, you can order the real kit. Try: {link}",
         media_kind="video", media_brief="Capture écran mobile 15 s : on tape un numéro, les zones se remplissent."),
    # ------------------------------------------------------------------ ENGAGEMENT / DEFI
    dict(name="Question : combien d'heures ?", category="engage", lang="en", platforms="reddit,facebook",
         hook="How many hours did your last paint by numbers take you? Mine: [X] h for a 40x50",
         body="Trying to figure out if 24 colours is the sweet spot for a first custom canvas. What do you all prefer?",
         media_kind="photo", media_brief="Toile en cours, moitié peinte, avec un minuteur/tasse de thé à côté."),
    dict(name="Défi mensuel — paint your pet", category="challenge", lang="en", platforms="reddit,facebook,social",
         hook="Paint-your-pet month: share your pet canvases (any brand!) 🐶🐱",
         body="Let's see them! I'll start with [PET'S NAME], 24 colours. (Founder of PaintIt here, but any brand is welcome — "
              "it's about the pets.)", media_kind="photo", media_brief="Toile du chien/chat posée à côté de l'animal."),
    dict(name="Quelle photo fonctionne le mieux ?", category="tips", lang="en", platforms="reddit,forum",
         hook="Tips: which photos make the best custom paint-by-numbers (from 100+ canvases)",
         body="1. Faces large in the frame. 2. Even light, no harsh shadows. 3. Simple background. 4. High resolution. "
              "5. For pets, eye level. Happy to look at your photo and tell you if it'll work.\n\n" + DISCLOSE_EN,
         media_kind="carousel", media_brief="Carrousel : bonne photo / mauvaise photo, côte à côte, avec un ✔/✘."),
    dict(name="Astuce : peindre les visages", category="tips", lang="en", platforms="reddit,social",
         hook="The one trick that makes faces look right in paint by numbers",
         body="Paint the eyes LAST, after the skin tones dry, and use the tip of a fine brush. Before/after in the 2nd image.",
         media_kind="beforeafter", media_brief="Gros plan visage : yeux non peints / yeux peints."),
    dict(name="Astuce : numéros qui transparaissent", category="tips", lang="en", platforms="reddit,forum",
         hook="Numbers showing through light colours? Here's how to fix it",
         body="Two thin coats beat one thick coat, and let the first dry fully. Light colours (whites, yellows) usually need "
              "two layers. What's your fix?", media_kind="photo", media_brief="Zone blanche une couche vs deux couches."),
    dict(name="Setup relaxant (ASMR)", category="engage", lang="en", platforms="social",
         hook="My Sunday reset: tea, music, 200 tiny numbered zones",
         body="Painting by numbers is the only time my phone stays face-down. What's yours? #relaxing #paintbynumbers",
         media_kind="video", media_brief="Vidéo calme 20 s, son du pinceau, lumière douce, vue de dessus."),
    # ------------------------------------------------------------------ FAMILLE / SENIORS / B2B
    dict(name="Activité enfant dès 4 ans", category="family", lang="en", platforms="facebook,social",
         hook="Rainy-day activity: my [AGE]-year-old painted our dog in 12 colours",
         body="Big zones, 12 colours, 45 minutes a day. She's so proud it's in the living room now. " + DISCLOSE_EN,
         media_kind="photo", media_brief="Enfant de dos qui peint (visage non visible), grosses zones visibles."),
    dict(name="Résidence seniors — atelier", category="b2b", lang="en", platforms="facebook,forum",
         hook="Activity coordinators: has anyone tried paint by numbers from residents' own photos?",
         body="I'm the founder of PaintIt and I'd love to offer a free trial kit to 2–3 care homes in exchange for honest "
              "feedback (12 colours, large zones). DM me if interested.", media_kind="text", media_brief=""),
    dict(name="Team building entreprise", category="b2b", lang="en", platforms="forum",
         hook="Team-building idea: paint your team photo, one canvas per person",
         body="Each person paints their own canvas from the team photo, then you hang them together. Founder of PaintIt here, "
              "happy to share how we'd set it up.", media_kind="photo", media_brief="Mur de 6 toiles peintes côte à côte."),
    # ------------------------------------------------------------------ UGC
    dict(name="Repartage client (avec accord)", category="ugc", lang="en", platforms="social",
         hook="[FIRST NAME] painted her [SUBJECT] and we can't stop looking at it 😍",
         body="Shared with [FIRST NAME]'s permission. Your photo could be next: {link}", media_kind="beforeafter",
         media_brief="Photo envoyée par la cliente + autorisation écrite conservée."),
    # ------------------------------------------------------------------ FRANCAIS
    dict(name="FR — Grands-parents", category="story", lang="fr", platforms="facebook,social",
         hook="J'ai transformé la seule photo de mes grands-parents qui dansent en peinture par numéros",
         body="[X] soirées de peinture. La photo d'origine date de [ANNÉE] (2e image).\n\n" + DISCLOSE_FR,
         media_kind="beforeafter", media_brief="Toile terminée + photo d'origine."),
    dict(name="FR — Cadeau fête des mères", category="season", lang="fr", platforms="social,facebook",
         hook="Le cadeau de fête des mères qu'elle va peindre elle-même 💐",
         body="Sa photo préférée devient une toile à peindre. Aperçu gratuit avant de commander : {link}",
         media_kind="video", media_brief="Mains d'une maman qui peint, révélation de la photo d'origine."),
    dict(name="FR — Activité enfants dès 4 ans", category="family", lang="fr", platforms="facebook,social",
         hook="Mercredi après-midi : ma fille de [ÂGE] ans a peint notre chat en 12 couleurs",
         body="Grandes zones, 12 couleurs, 45 minutes par jour. Elle est trop fière ! " + DISCLOSE_FR,
         media_kind="photo", media_brief="Enfant de dos, toile en cours."),
    dict(name="FR — Atelier EHPAD", category="b2b", lang="fr", platforms="facebook,forum",
         hook="Animateurs en EHPAD : un atelier peinture à partir des photos de vos résidents ?",
         body="Je suis le fondateur de PaintIt. Je propose un kit d'essai gratuit à 2-3 résidences en échange de vos retours "
              "(12 couleurs, grandes zones). Écrivez-moi en privé.", media_kind="text", media_brief=""),
    dict(name="FR — Avant / après (carrousel)", category="beforeafter", lang="fr", platforms="social",
         hook="Photo → toile numérotée → tableau peint ✨",
         body="Ta photo peut devenir ton prochain tableau. Essai gratuit en ligne : lien en bio. #peintureparnumero #cadeaupersonnalise",
         media_kind="carousel", media_brief="Carrousel 3 étapes, même cadrage."),
    # ------------------------------------------------------------------ DEUTSCH
    dict(name="DE — Großeltern", category="story", lang="de", platforms="facebook,social",
         hook="Ich habe das einzige Foto meiner tanzenden Großeltern in ein Malen-nach-Zahlen verwandelt",
         body="[X] Abende gemalt, Originalfoto von [JAHR] im 2. Bild.\n\nGanz offen: Ich bin der Gründer von PaintIt, "
              "dem Tool, das ich benutzt habe.", media_kind="beforeafter", media_brief="Fertiges Bild + Originalfoto."),
    dict(name="DE — Geschenkidee Muttertag", category="season", lang="de", platforms="social,facebook",
         hook="Das Muttertagsgeschenk, das sie selbst malt 💐",
         body="Ihr Lieblingsfoto als Malen-nach-Zahlen-Leinwand. Kostenlose Vorschau: {link}", media_kind="video",
         media_brief="Hände einer Mutter beim Malen, dann das Originalfoto."),
    dict(name="DE — Tipp: welche Fotos?", category="tips", lang="de", platforms="facebook,forum",
         hook="Welche Fotos eignen sich am besten für Malen nach Zahlen? 5 Tipps",
         body="1. Gesichter groß im Bild. 2. Gleichmäßiges Licht. 3. Ruhiger Hintergrund. 4. Hohe Auflösung. 5. Haustiere "
              "auf Augenhöhe. (Gründer von PaintIt hier – ich schaue mir gerne euer Foto an.)", media_kind="carousel",
         media_brief="Gutes vs. schlechtes Foto nebeneinander."),
    # ------------------------------------------------------------------ ESPANOL
    dict(name="ES — Abuelos", category="story", lang="es", platforms="facebook,social",
         hook="Convertí la única foto de mis abuelos bailando en una pintura por números",
         body="[X] tardes pintando. Foto original de [AÑO] en la 2.ª imagen.\n\nCon total transparencia: soy el fundador de "
              "PaintIt, la herramienta que usé.", media_kind="beforeafter", media_brief="Cuadro terminado + foto original."),
    dict(name="ES — Regalo personalizado", category="gift", lang="es", platforms="social,facebook",
         hook="El regalo que pintan ellos mismos 🎨",
         body="Su foto favorita convertida en un lienzo para pintar por números. Vista previa gratis: {link}",
         media_kind="video", media_brief="Desembalaje del kit + reacción (con permiso)."),
    # ------------------------------------------------------------------ ITALIANO
    dict(name="IT — Nonni", category="story", lang="it", platforms="facebook,social",
         hook="Ho trasformato l'unica foto dei miei nonni che ballano in un dipinto con i numeri",
         body="[X] serate di pittura. Foto originale del [ANNO] nella 2ª immagine.\n\nIn tutta trasparenza: sono il "
              "fondatore di PaintIt, lo strumento che ho usato.", media_kind="beforeafter", media_brief="Quadro finito + foto originale."),
    dict(name="IT — Regalo per la mamma", category="season", lang="it", platforms="social,facebook",
         hook="Il regalo per la festa della mamma che dipingerà lei stessa 💐",
         body="La sua foto preferita diventa una tela da dipingere. Anteprima gratuita: {link}", media_kind="video",
         media_brief="Mani di una mamma che dipinge, poi la foto originale."),
    # ------------------------------------------------------------------ AUSTRALIE / US saisonnier
    dict(name="AU — Father's Day (septembre)", category="season", lang="en", platforms="social,facebook",
         hook="Father's Day in Australia is in September — here's a gift Dad will actually use",
         body="His favourite photo (the ute, the dog, the fishing trip) as a paint-by-numbers. Free preview: {link}",
         media_kind="photo", media_brief="Toile d'un bateau/voiture/chien, ambiance australienne."),
    dict(name="US — Thanksgiving family photo", category="season", lang="en", platforms="social,facebook",
         hook="This year's Thanksgiving photo, painted by everyone at the table",
         body="One canvas, the whole family takes turns on a zone after dinner. Free preview: {link}",
         media_kind="video", media_brief="Plusieurs mains qui peignent tour à tour la même toile."),
    dict(name="Saint-Valentin couple", category="season", lang="en", platforms="social",
         hook="Valentine's date idea: paint your favourite photo together ❤️",
         body="Two brushes, one canvas, zero screens. Free preview: {link}", media_kind="video",
         media_brief="Deux mains qui peignent, bougie, ambiance tamisée."),
    dict(name="Témoignage de patience (expert)", category="challenge", lang="en", platforms="reddit,social",
         hook="48 colours, 2,700 zones, 6 weeks. My hardest paint by numbers yet",
         body="Expert density for a portrait. Worth every hour. AMA about the process. " + DISCLOSE_EN,
         media_kind="carousel", media_brief="Toile finie + macro sur les zones fines + capture du compteur de zones."),
]


def seed_library():
    from .models import PostTemplate
    n = 0
    for row in LIBRARY:
        _o, created = PostTemplate.objects.get_or_create(name=row["name"], defaults=row)
        n += created
    return n
