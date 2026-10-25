"""FAQ publique (page /faq/ + donnees structurees FAQPage).
Les reponses reprennent les VRAIES valeurs de la boutique (formats, prix, delais, couleurs) :
elles se mettent a jour toutes seules quand la grille tarifaire change."""
from django.utils.translation import get_language


def _ctx():
    from .models import KitFormat, Pricing
    p = Pricing.get()
    fmts = list(KitFormat.objects.filter(available=True))
    prices = [float(f.price) for f in fmts] or [p.format_price("40x50")]
    colors = p.available_colors()
    return {
        "dmin": p.delivery_days_min, "dmax": p.delivery_days_max,
        "formats": ", ".join(f.label for f in fmts) or "40 x 50 cm",
        "pmin": ("%.2f" % min(prices)).replace(".", ","),
        "colors": ", ".join(str(c) for c in colors[:-1]) + (" / " if len(colors) > 1 else "") + str(colors[-1]),
        "brushes": ("%.2f" % p.brushes_price).replace(".", ","),
        "origin": _origin(),
    }


def _origin():
    from django.utils.translation import gettext as _
    try:
        from .models import CompanyInfo
        o = CompanyInfo.get().production_origin or ""
    except Exception:
        o = ""
    return _("Europe") if o.strip().lower() == "europe" else o


FAQ = {
    'fr': [
        ('Comment fonctionne PaintIt ?',
         "Vous envoyez une photo, notre algorithme la transforme en moins d'une minute en toile à peindre par numéros : zones numérotées, palette de couleurs et aperçu colorié. Vous pouvez la peindre gratuitement en ligne, puis commander le kit complet pour la peindre chez vous."),
        ("Pourquoi une toile personnalisée plutôt qu'un modèle de catalogue ?",
         "Parce qu'elle raconte votre histoire : votre enfant, votre animal, votre plus beau voyage. Notre algorithme calcule une toile unique pour votre photo, et vous la testez gratuitement avant de commander."),
        ('Quelles photos donnent le meilleur résultat ?',
         'Une photo nette, bien éclairée, avec un sujet principal clair : portrait, animal, paysage, souvenir de voyage. Évitez les photos floues, très sombres ou en très basse résolution. Vous pouvez recadrer la photo avant la génération.'),
        ('Que contient le kit ?',
         "Une toile numérotée imprimée au format choisi, les pots de peinture numérotés correspondant à chaque couleur, un poster de référence avec l'aperçu et la palette. Un set de pinceaux est proposé en option ({brushes} €)."),
        ('Comment sont fabriqués les kits ?',
         'Les kits sont fabriqués à la demande par nos ateliers partenaires, audités selon nos exigences de qualité, à partir de toiles et de peintures de qualité, puis contrôlés et livrés gratuitement. Les couleurs de votre toile sont choisies parmi de vraies peintures disponibles chez notre atelier partenaire.'),
        ('Quels formats et combien de couleurs ?',
         'Formats disponibles : {formats}. Vous choisissez {colors} couleurs : plus il y a de couleurs, plus la toile est détaillée et fidèle à la photo. Les prix démarrent à {pmin} €, livraison offerte.'),
        ('Quels sont les délais de livraison ?',
         'Chaque kit est fabriqué à la demande puis expédié : comptez {dmin} à {dmax} jours ouvrés. La livraison est offerte.'),
        ("Puis-je essayer avant d'acheter ?",
         "Oui. L'aperçu est gratuit et sans inscription, et vous pouvez peindre votre toile en ligne gratuitement pendant 5 minutes. Pour la garder dans « Mes toiles » et continuer quand vous voulez, la sauvegarde coûte 0,99 €."),
        ('Puis-je imprimer ma toile moi-même ?',
         "Oui. Depuis l'aperçu ou le jeu, « Télécharger ma toile » (0,99 €) donne la toile numérotée en haute définition (TIFF) au format de votre choix et le poster des couleurs, à imprimer chez vous ou chez un imprimeur. La toile digitale jouable est incluse, et vous pouvez retélécharger à tout moment depuis « Mes toiles », dans un autre format si vous le souhaitez."),
        ('Faut-il savoir dessiner ?',
         "Non. Chaque zone porte un numéro qui correspond à un pot de peinture : il suffit de remplir les zones. C'est une activité relaxante, accessible aux débutants comme aux plus expérimentés."),
        ('À partir de quel âge peut-on peindre ?',
         "Dès 4 ans avec 12 couleurs : grandes zones, peu de teintes, c'est très accessible pour les enfants comme pour les seniors. 24 couleurs offrent le bon équilibre, 36 couleurs et plus s'adressent aux passionnés."),
        ('Puis-je modifier ma toile avant de commander ?',
         "Oui. Depuis l'aperçu, le bouton « Modifier » permet de changer le nombre de couleurs, l'orientation ou le cadrage, sans renvoyer votre photo."),
        ('Comment payer et est-ce sécurisé ?',
         'Le paiement par carte est traité par Stripe, leader du paiement en ligne. Nous ne stockons aucune donnée bancaire. Une facture vous est envoyée par e-mail.'),
        ('Puis-je retourner mon kit ?',
         "Le kit étant fabriqué à partir de votre photo, il n'est pas soumis au droit de rétractation. En revanche, un produit défectueux ou abîmé est remplacé ou remboursé : contactez-nous avec une photo."),
        ('Que deviennent mes photos ?',
         'Elles servent uniquement à fabriquer votre toile. Elles ne sont jamais publiées ni revendues, et vous pouvez demander leur suppression à tout moment.'),
    ],
    'en': [
        ('How does PaintIt work?',
         'Upload a photo and our algorithm turns it into a paint-by-numbers canvas in under a minute: numbered zones, a colour palette and a coloured preview. Paint it online for free, then order the full kit to paint it at home.'),
        ('Why a personalised canvas rather than a catalogue design?',
         'Because it tells your story: your child, your pet, your best trip. Our algorithm computes a unique canvas for your photo, and you can test it for free before ordering.'),
        ('Which photos work best?',
         'A sharp, well-lit photo with a clear main subject: portrait, pet, landscape, travel memory. Avoid blurry, very dark or very low-resolution photos. You can crop the photo before generating.'),
        ("What's in the kit?",
         'A numbered canvas printed in your chosen size, numbered paint pots for every colour and a reference poster with the preview and palette. A brush set is available as an option (€{brushes}).'),
        ('How are the kits made?',
         'Kits are made to order by our partner workshops, audited against our quality standards, with quality canvas and paints, then checked and shipped for free. Your canvas colours are picked from real paints available at our workshop.'),
        ('Which sizes and how many colours?',
         'Available sizes: {formats}. Choose {colors} colours: the more colours, the more detailed and faithful the canvas. Prices start at €{pmin}, free shipping.'),
        ('How long is delivery?',
         'Each kit is made to order and then shipped: allow {dmin} to {dmax} business days. Shipping is free.'),
        ('Can I try before buying?',
         'Yes. The preview is free with no sign-up, and you can paint your canvas online for free for 5 minutes. Saving it to “My canvases” to keep painting anytime costs €0.99.'),
        ('Can I print my canvas myself?',
         'Yes. From the preview or the game, “Download my canvas” (€0.99) gives you the numbered canvas in high definition (TIFF) in the size of your choice plus the colour poster, to print at home or at a print shop. The playable digital canvas is included, and you can download it again anytime from “My canvases”, in another size if you like.'),
        ('Do I need to know how to draw?',
         "No. Each zone has a number matching a paint pot: just fill in the zones. It's a relaxing activity for beginners and experienced painters alike."),
        ('From what age can you paint?',
         'From age 4 with 12 colours: large zones and few shades make it very accessible for children and seniors alike. 24 colours offer the right balance; 36 colours and more are for enthusiasts.'),
        ('Can I change my canvas before ordering?',
         'Yes. From the preview, the “Edit” button lets you change the number of colours, orientation or framing without uploading your photo again.'),
        ('How do I pay, and is it secure?',
         'Card payments are processed by Stripe, the online payment leader. We never store card details. An invoice is sent by email.'),
        ('Can I return my kit?',
         'As the kit is made from your own photo, it is not subject to the right of withdrawal. A faulty or damaged product is replaced or refunded: contact us with a photo.'),
        ('What happens to my photos?',
         'They are only used to make your canvas. They are never published or sold, and you can ask for their deletion at any time.'),
    ],
    'de': [
        ('Wie funktioniert PaintIt?',
         'Sie laden ein Foto hoch, unser Algorithmus verwandelt es in weniger als einer Minute in ein Malen-nach-Zahlen-Bild: nummerierte Felder, Farbpalette und farbige Vorschau. Malen Sie es kostenlos online und bestellen Sie dann das komplette Set, um es zu Hause zu malen.'),
        ('Warum ein personalisiertes Bild statt eines Katalogmotivs?',
         'Weil es Ihre Geschichte erzählt: Ihr Kind, Ihr Haustier, Ihre schönste Reise. Unser Algorithmus berechnet ein einzigartiges Bild für Ihr Foto, und Sie testen es kostenlos vor der Bestellung.'),
        ('Welche Fotos eignen sich am besten?',
         'Ein scharfes, gut beleuchtetes Foto mit klarem Hauptmotiv: Porträt, Haustier, Landschaft, Reiseerinnerung. Vermeiden Sie unscharfe, sehr dunkle oder sehr kleine Fotos. Sie können das Foto vor der Erstellung zuschneiden.'),
        ('Was ist im Set enthalten?',
         'Eine nummerierte Leinwand im gewählten Format, nummerierte Farbtöpfe für jede Farbe und ein Referenzposter mit Vorschau und Palette. Ein Pinselset ist optional erhältlich ({brushes} €).'),
        ('Wie werden die Sets hergestellt?',
         'Die Sets werden auf Bestellung von unseren nach unseren Qualitätsanforderungen geprüften Partnerwerkstätten aus hochwertiger Leinwand und Farben hergestellt, geprüft und kostenlos geliefert. Die Farben Ihres Bildes werden aus echten, in unserer Werkstatt verfügbaren Farben gewählt.'),
        ('Welche Formate und wie viele Farben?',
         'Verfügbare Formate: {formats}. Sie wählen {colors} Farben: Je mehr Farben, desto detaillierter das Bild. Preise ab {pmin} €, kostenloser Versand.'),
        ('Wie lange dauert die Lieferung?',
         'Jedes Set wird auf Bestellung gefertigt und dann versendet: Rechnen Sie mit {dmin} bis {dmax} Werktagen. Der Versand ist kostenlos.'),
        ('Kann ich vor dem Kauf testen?',
         'Ja. Die Vorschau ist kostenlos und ohne Anmeldung, und Sie können Ihr Bild 5 Minuten lang kostenlos online malen. Das Speichern unter „Meine Bilder“ kostet 0,99 €.'),
        ('Kann ich mein Bild selbst ausdrucken?',
         'Ja. In der Vorschau oder im Spiel liefert „Mein Bild herunterladen“ (0,99 €) die nummerierte Leinwand in hoher Auflösung (TIFF) im Format Ihrer Wahl und das Farbposter, zum Drucken zu Hause oder in einer Druckerei. Die spielbare digitale Leinwand ist inklusive, und Sie können jederzeit unter „Meine Bilder“ erneut herunterladen, auch in einem anderen Format.'),
        ('Muss ich zeichnen können?',
         'Nein. Jedes Feld hat eine Nummer, die einem Farbtopf entspricht: Sie füllen einfach die Felder aus. Eine entspannende Beschäftigung für Anfänger und Erfahrene.'),
        ('Ab welchem Alter kann man malen?',
         'Ab 4 Jahren mit 12 Farben: große Felder und wenige Farbtöne, sehr zugänglich für Kinder und Senioren. 24 Farben bieten die richtige Balance, 36 Farben und mehr sind für Enthusiasten.'),
        ('Kann ich mein Bild vor der Bestellung ändern?',
         'Ja. In der Vorschau können Sie mit „Bearbeiten“ Farbanzahl, Ausrichtung oder Ausschnitt ändern, ohne Ihr Foto erneut hochzuladen.'),
        ('Wie bezahle ich, und ist das sicher?',
         'Kartenzahlungen werden von Stripe abgewickelt. Wir speichern keine Kartendaten. Sie erhalten eine Rechnung per E-Mail.'),
        ('Kann ich mein Set zurückgeben?',
         'Da das Set aus Ihrem Foto gefertigt wird, besteht kein Widerrufsrecht. Ein fehlerhaftes oder beschädigtes Produkt wird ersetzt oder erstattet: Kontaktieren Sie uns mit einem Foto.'),
        ('Was passiert mit meinen Fotos?',
         'Sie dienen nur der Herstellung Ihres Bildes. Sie werden nie veröffentlicht oder verkauft, und Sie können jederzeit deren Löschung verlangen.'),
    ],
    'es': [
        ('¿Cómo funciona PaintIt?',
         'Subes una foto y nuestro algoritmo la convierte en menos de un minuto en un lienzo para pintar por números: zonas numeradas, paleta de colores y vista previa en color. Píntalo gratis en línea y luego pide el kit completo para pintarlo en casa.'),
        ('¿Por qué un lienzo personalizado y no un modelo de catálogo?',
         'Porque cuenta tu historia: tu hijo, tu mascota, tu mejor viaje. Nuestro algoritmo calcula un lienzo único para tu foto, y lo pruebas gratis antes de pedirlo.'),
        ('¿Qué fotos dan mejor resultado?',
         'Una foto nítida, bien iluminada y con un motivo principal claro: retrato, mascota, paisaje, recuerdo de viaje. Evita fotos borrosas, muy oscuras o de muy baja resolución. Puedes recortar la foto antes de generarla.'),
        ('¿Qué incluye el kit?',
         'Un lienzo numerado impreso en el formato elegido, botes de pintura numerados para cada color y un póster de referencia con la vista previa y la paleta. Set de pinceles opcional ({brushes} €).'),
        ('¿Cómo se fabrican los kits?',
         'Los kits se fabrican bajo pedido en nuestros talleres asociados, auditados según nuestras exigencias de calidad, con lienzo y pinturas de calidad, se revisan y se envían gratis. Los colores de tu lienzo se eligen entre pinturas reales disponibles en nuestro taller.'),
        ('¿Qué formatos y cuántos colores?',
         'Formatos disponibles: {formats}. Eliges {colors} colores: cuantos más colores, más detallado el lienzo. Precios desde {pmin} €, envío gratuito.'),
        ('¿Cuánto tarda la entrega?',
         'Cada kit se fabrica bajo pedido y luego se envía: cuenta de {dmin} a {dmax} días laborables. El envío es gratuito.'),
        ('¿Puedo probar antes de comprar?',
         'Sí. La vista previa es gratuita y sin registro, y puedes pintar tu lienzo en línea gratis durante 5 minutos. Guardarlo en «Mis lienzos» cuesta 0,99 €.'),
        ('¿Puedo imprimir mi lienzo yo mismo?',
         'Sí. Desde la vista previa o el juego, «Descargar mi lienzo» (0,99 €) te da el lienzo numerado en alta definición (TIFF) en el formato que elijas y el póster de colores, para imprimir en casa o en una imprenta. Incluye el lienzo digital jugable, y puedes volver a descargarlo cuando quieras desde «Mis lienzos», en otro formato si lo deseas.'),
        ('¿Hace falta saber dibujar?',
         'No. Cada zona tiene un número que corresponde a un bote de pintura: solo hay que rellenar las zonas. Una actividad relajante para principiantes y expertos.'),
        ('¿A partir de qué edad se puede pintar?',
         'Desde los 4 años con 12 colores: zonas grandes y pocos tonos, muy accesible para niños y mayores. 24 colores ofrecen el equilibrio justo; 36 colores o más son para apasionados.'),
        ('¿Puedo modificar mi lienzo antes de pedir?',
         'Sí. Desde la vista previa, el botón «Editar» permite cambiar el número de colores, la orientación o el encuadre sin volver a subir la foto.'),
        ('¿Cómo pago y es seguro?',
         'Los pagos con tarjeta los procesa Stripe. No guardamos ningún dato bancario. Recibirás una factura por correo.'),
        ('¿Puedo devolver mi kit?',
         'Al estar fabricado con tu foto, el kit no está sujeto al derecho de desistimiento. Un producto defectuoso o dañado se sustituye o reembolsa: contáctanos con una foto.'),
        ('¿Qué pasa con mis fotos?',
         'Solo sirven para fabricar tu lienzo. Nunca se publican ni se venden, y puedes pedir su eliminación en cualquier momento.'),
    ],
    'it': [
        ('Come funziona PaintIt?',
         'Invii una foto e il nostro algoritmo la trasforma in meno di un minuto in una tela da dipingere con i numeri: zone numerate, tavolozza di colori e anteprima a colori. Dipingila gratis online, poi ordina il kit completo per dipingerla a casa.'),
        ('Perché una tela personalizzata invece di un modello da catalogo?',
         'Perché racconta la tua storia: tuo figlio, il tuo animale, il tuo viaggio più bello. Il nostro algoritmo calcola una tela unica per la tua foto, e la provi gratis prima di ordinare.'),
        ('Quali foto danno il risultato migliore?',
         'Una foto nitida, ben illuminata, con un soggetto principale chiaro: ritratto, animale, paesaggio, ricordo di viaggio. Evita foto sfocate, molto scure o a bassissima risoluzione. Puoi ritagliare la foto prima della generazione.'),
        ('Cosa contiene il kit?',
         'Una tela numerata stampata nel formato scelto, i vasetti di colore numerati per ogni colore e un poster di riferimento con anteprima e tavolozza. Set di pennelli opzionale ({brushes} €).'),
        ('Come vengono prodotti i kit?',
         'I kit sono prodotti su ordinazione dai nostri laboratori partner, verificati secondo i nostri standard di qualità, con tela e colori di qualità, poi controllati e spediti gratuitamente. I colori della tua tela sono scelti tra veri colori disponibili nel nostro laboratorio.'),
        ('Quali formati e quanti colori?',
         'Formati disponibili: {formats}. Scegli {colors} colori: più colori ci sono, più la tela è dettagliata. Prezzi a partire da {pmin} €, spedizione gratuita.'),
        ('Quali sono i tempi di consegna?',
         'Ogni kit è prodotto su ordinazione e poi spedito: calcola da {dmin} a {dmax} giorni lavorativi. La spedizione è gratuita.'),
        ('Posso provare prima di acquistare?',
         "Sì. L'anteprima è gratuita e senza registrazione, e puoi dipingere la tua tela online gratis per 5 minuti. Salvarla in « Le mie tele » costa 0,99 €."),
        ('Posso stampare la mia tela da solo?',
         "Sì. Dall'anteprima o dal gioco, «Scarica la mia tela» (0,99 €) ti dà la tela numerata in alta definizione (TIFF) nel formato che preferisci e il poster dei colori, da stampare a casa o in tipografia. La tela digitale giocabile è inclusa e puoi riscaricarla quando vuoi da «Le mie tele», anche in un altro formato."),
        ('Bisogna saper disegnare?',
         "No. Ogni zona ha un numero che corrisponde a un vasetto di colore: basta riempire le zone. Un'attività rilassante per principianti ed esperti."),
        ('Da che età si può dipingere?',
         'Dai 4 anni con 12 colori: zone grandi e poche tonalità, molto accessibile per bambini e anziani. 24 colori offrono il giusto equilibrio; 36 colori e oltre sono per gli appassionati.'),
        ('Posso modificare la tela prima di ordinare?',
         "Sì. Dall'anteprima, il pulsante « Modifica » permette di cambiare numero di colori, orientamento o inquadratura senza reinviare la foto."),
        ('Come si paga ed è sicuro?',
         'I pagamenti con carta sono gestiti da Stripe. Non conserviamo alcun dato bancario. Riceverai una fattura via e-mail.'),
        ('Posso restituire il kit?',
         'Essendo realizzato dalla tua foto, il kit non è soggetto al diritto di recesso. Un prodotto difettoso o danneggiato viene sostituito o rimborsato: contattaci con una foto.'),
        ('Che fine fanno le mie foto?',
         'Servono solo a realizzare la tua tela. Non vengono mai pubblicate né vendute, e puoi chiederne la cancellazione in qualsiasi momento.'),
    ],
}


def faq_items(lang=None):
    lang = (lang or get_language() or "fr")[:2]
    items = FAQ.get(lang, FAQ["en"])
    c = _ctx()
    return [{"q": q, "a": a.format(**c)} for q, a in items]
