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
    }


FAQ = {
    "fr": [
        ("Comment fonctionne PaintIt ?",
         "Vous envoyez une photo, notre algorithme la transforme en moins d'une minute en toile à peindre par numéros : "
         "zones numérotées, palette de couleurs et aperçu colorié. Vous pouvez la peindre gratuitement en ligne, "
         "puis commander le kit complet pour la peindre chez vous."),
        ("Quelles photos donnent le meilleur résultat ?",
         "Une photo nette, bien éclairée, avec un sujet principal clair : portrait, animal, paysage, souvenir de voyage. "
         "Évitez les photos floues, très sombres ou en très basse résolution. Vous pouvez recadrer la photo avant la génération."),
        ("Que contient le kit ?",
         "Une toile numérotée imprimée au format choisi, les pots de peinture numérotés correspondant à chaque couleur, "
         "un poster de référence avec l'aperçu et la palette. Un set de pinceaux est proposé en option ({brushes} €)."),
        ("Quels formats et combien de couleurs ?",
         "Formats disponibles : {formats}. Vous choisissez {colors} couleurs : plus il y a de couleurs, plus la toile est "
         "détaillée et fidèle à la photo. Les prix démarrent à {pmin} €, livraison offerte."),
        ("Quels sont les délais de livraison ?",
         "Chaque kit est fabriqué à la demande puis expédié : comptez {dmin} à {dmax} jours ouvrés. "
         "La livraison est offerte."),
        ("Puis-je essayer avant d'acheter ?",
         "Oui. L'aperçu est gratuit et sans inscription, et vous pouvez peindre votre toile en ligne gratuitement pendant 5 minutes. "
         "Pour la garder dans « Mes toiles » et continuer quand vous voulez, la sauvegarde coûte 0,99 €."),
        ("Faut-il savoir dessiner ?",
         "Non. Chaque zone porte un numéro qui correspond à un pot de peinture : il suffit de remplir les zones. "
         "C'est une activité relaxante, accessible aux débutants comme aux plus expérimentés."),
        ("Puis-je modifier ma toile avant de commander ?",
         "Oui. Depuis l'aperçu, le bouton « Modifier » permet de changer le nombre de couleurs, l'orientation ou le cadrage, "
         "sans renvoyer votre photo."),
        ("Comment payer et est-ce sécurisé ?",
         "Le paiement par carte est traité par Stripe, leader du paiement en ligne. Nous ne stockons aucune donnée bancaire. "
         "Une facture vous est envoyée par e-mail."),
        ("Puis-je retourner mon kit ?",
         "Le kit étant fabriqué à partir de votre photo, il n'est pas soumis au droit de rétractation. "
         "En revanche, un produit défectueux ou abîmé est remplacé ou remboursé : contactez-nous avec une photo."),
        ("Que deviennent mes photos ?",
         "Elles servent uniquement à fabriquer votre toile. Elles ne sont jamais publiées ni revendues, "
         "et vous pouvez demander leur suppression à tout moment."),
    ],
    "en": [
        ("How does PaintIt work?",
         "Upload a photo and our algorithm turns it into a paint-by-numbers canvas in under a minute: numbered zones, a colour "
         "palette and a coloured preview. Paint it online for free, then order the full kit to paint it at home."),
        ("Which photos work best?",
         "A sharp, well-lit photo with a clear main subject: portrait, pet, landscape, travel memory. Avoid blurry, very dark "
         "or very low-resolution photos. You can crop the photo before generating."),
        ("What's in the kit?",
         "A numbered canvas printed in your chosen size, numbered paint pots for every colour and a reference poster with the "
         "preview and palette. A brush set is available as an option (€{brushes})."),
        ("Which sizes and how many colours?",
         "Available sizes: {formats}. Choose {colors} colours: the more colours, the more detailed and faithful the canvas. "
         "Prices start at €{pmin}, free shipping."),
        ("How long is delivery?",
         "Each kit is made to order and then shipped: allow {dmin} to {dmax} business days. Shipping is free."),
        ("Can I try before buying?",
         "Yes. The preview is free with no sign-up, and you can paint your canvas online for free for 5 minutes. "
         "Saving it to “My canvases” to keep painting anytime costs €0.99."),
        ("Do I need to know how to draw?",
         "No. Each zone has a number matching a paint pot: just fill in the zones. It's a relaxing activity for beginners and "
         "experienced painters alike."),
        ("Can I change my canvas before ordering?",
         "Yes. From the preview, the “Edit” button lets you change the number of colours, orientation or framing without "
         "uploading your photo again."),
        ("How do I pay, and is it secure?",
         "Card payments are processed by Stripe, the online payment leader. We never store card details. An invoice is sent "
         "by email."),
        ("Can I return my kit?",
         "As the kit is made from your own photo, it is not subject to the right of withdrawal. A faulty or damaged product is "
         "replaced or refunded: contact us with a photo."),
        ("What happens to my photos?",
         "They are only used to make your canvas. They are never published or sold, and you can ask for their deletion at any time."),
    ],
    "de": [
        ("Wie funktioniert PaintIt?",
         "Sie laden ein Foto hoch, unser Algorithmus verwandelt es in weniger als einer Minute in ein Malen-nach-Zahlen-Bild: "
         "nummerierte Felder, Farbpalette und farbige Vorschau. Malen Sie es kostenlos online und bestellen Sie dann das "
         "komplette Set, um es zu Hause zu malen."),
        ("Welche Fotos eignen sich am besten?",
         "Ein scharfes, gut beleuchtetes Foto mit klarem Hauptmotiv: Porträt, Haustier, Landschaft, Reiseerinnerung. "
         "Vermeiden Sie unscharfe, sehr dunkle oder sehr kleine Fotos. Sie können das Foto vor der Erstellung zuschneiden."),
        ("Was ist im Set enthalten?",
         "Eine nummerierte Leinwand im gewählten Format, nummerierte Farbtöpfe für jede Farbe und ein Referenzposter mit "
         "Vorschau und Palette. Ein Pinselset ist optional erhältlich ({brushes} €)."),
        ("Welche Formate und wie viele Farben?",
         "Verfügbare Formate: {formats}. Sie wählen {colors} Farben: Je mehr Farben, desto detaillierter das Bild. "
         "Preise ab {pmin} €, kostenloser Versand."),
        ("Wie lange dauert die Lieferung?",
         "Jedes Set wird auf Bestellung gefertigt und dann versendet: Rechnen Sie mit {dmin} bis {dmax} Werktagen. "
         "Der Versand ist kostenlos."),
        ("Kann ich vor dem Kauf testen?",
         "Ja. Die Vorschau ist kostenlos und ohne Anmeldung, und Sie können Ihr Bild 5 Minuten lang kostenlos online malen. "
         "Das Speichern unter „Meine Bilder“ kostet 0,99 €."),
        ("Muss ich zeichnen können?",
         "Nein. Jedes Feld hat eine Nummer, die einem Farbtopf entspricht: Sie füllen einfach die Felder aus. "
         "Eine entspannende Beschäftigung für Anfänger und Erfahrene."),
        ("Kann ich mein Bild vor der Bestellung ändern?",
         "Ja. In der Vorschau können Sie mit „Bearbeiten“ Farbanzahl, Ausrichtung oder Ausschnitt ändern, ohne Ihr Foto "
         "erneut hochzuladen."),
        ("Wie bezahle ich, und ist das sicher?",
         "Kartenzahlungen werden von Stripe abgewickelt. Wir speichern keine Kartendaten. Sie erhalten eine Rechnung per E-Mail."),
        ("Kann ich mein Set zurückgeben?",
         "Da das Set aus Ihrem Foto gefertigt wird, besteht kein Widerrufsrecht. Ein fehlerhaftes oder beschädigtes Produkt wird "
         "ersetzt oder erstattet: Kontaktieren Sie uns mit einem Foto."),
        ("Was passiert mit meinen Fotos?",
         "Sie dienen nur der Herstellung Ihres Bildes. Sie werden nie veröffentlicht oder verkauft, und Sie können jederzeit "
         "deren Löschung verlangen."),
    ],
    "es": [
        ("¿Cómo funciona PaintIt?",
         "Subes una foto y nuestro algoritmo la convierte en menos de un minuto en un lienzo para pintar por números: zonas "
         "numeradas, paleta de colores y vista previa en color. Píntalo gratis en línea y luego pide el kit completo para "
         "pintarlo en casa."),
        ("¿Qué fotos dan mejor resultado?",
         "Una foto nítida, bien iluminada y con un motivo principal claro: retrato, mascota, paisaje, recuerdo de viaje. "
         "Evita fotos borrosas, muy oscuras o de muy baja resolución. Puedes recortar la foto antes de generarla."),
        ("¿Qué incluye el kit?",
         "Un lienzo numerado impreso en el formato elegido, botes de pintura numerados para cada color y un póster de "
         "referencia con la vista previa y la paleta. Set de pinceles opcional ({brushes} €)."),
        ("¿Qué formatos y cuántos colores?",
         "Formatos disponibles: {formats}. Eliges {colors} colores: cuantos más colores, más detallado el lienzo. "
         "Precios desde {pmin} €, envío gratuito."),
        ("¿Cuánto tarda la entrega?",
         "Cada kit se fabrica bajo pedido y luego se envía: cuenta de {dmin} a {dmax} días laborables. El envío es gratuito."),
        ("¿Puedo probar antes de comprar?",
         "Sí. La vista previa es gratuita y sin registro, y puedes pintar tu lienzo en línea gratis durante 5 minutos. "
         "Guardarlo en «Mis lienzos» cuesta 0,99 €."),
        ("¿Hace falta saber dibujar?",
         "No. Cada zona tiene un número que corresponde a un bote de pintura: solo hay que rellenar las zonas. "
         "Una actividad relajante para principiantes y expertos."),
        ("¿Puedo modificar mi lienzo antes de pedir?",
         "Sí. Desde la vista previa, el botón «Editar» permite cambiar el número de colores, la orientación o el encuadre "
         "sin volver a subir la foto."),
        ("¿Cómo pago y es seguro?",
         "Los pagos con tarjeta los procesa Stripe. No guardamos ningún dato bancario. Recibirás una factura por correo."),
        ("¿Puedo devolver mi kit?",
         "Al estar fabricado con tu foto, el kit no está sujeto al derecho de desistimiento. Un producto defectuoso o dañado "
         "se sustituye o reembolsa: contáctanos con una foto."),
        ("¿Qué pasa con mis fotos?",
         "Solo sirven para fabricar tu lienzo. Nunca se publican ni se venden, y puedes pedir su eliminación en cualquier momento."),
    ],
    "it": [
        ("Come funziona PaintIt?",
         "Invii una foto e il nostro algoritmo la trasforma in meno di un minuto in una tela da dipingere con i numeri: zone "
         "numerate, tavolozza di colori e anteprima a colori. Dipingila gratis online, poi ordina il kit completo per dipingerla a casa."),
        ("Quali foto danno il risultato migliore?",
         "Una foto nitida, ben illuminata, con un soggetto principale chiaro: ritratto, animale, paesaggio, ricordo di viaggio. "
         "Evita foto sfocate, molto scure o a bassissima risoluzione. Puoi ritagliare la foto prima della generazione."),
        ("Cosa contiene il kit?",
         "Una tela numerata stampata nel formato scelto, i vasetti di colore numerati per ogni colore e un poster di "
         "riferimento con anteprima e tavolozza. Set di pennelli opzionale ({brushes} €)."),
        ("Quali formati e quanti colori?",
         "Formati disponibili: {formats}. Scegli {colors} colori: più colori ci sono, più la tela è dettagliata. "
         "Prezzi a partire da {pmin} €, spedizione gratuita."),
        ("Quali sono i tempi di consegna?",
         "Ogni kit è prodotto su ordinazione e poi spedito: calcola da {dmin} a {dmax} giorni lavorativi. La spedizione è gratuita."),
        ("Posso provare prima di acquistare?",
         "Sì. L'anteprima è gratuita e senza registrazione, e puoi dipingere la tua tela online gratis per 5 minuti. "
         "Salvarla in « Le mie tele » costa 0,99 €."),
        ("Bisogna saper disegnare?",
         "No. Ogni zona ha un numero che corrisponde a un vasetto di colore: basta riempire le zone. "
         "Un'attività rilassante per principianti ed esperti."),
        ("Posso modificare la tela prima di ordinare?",
         "Sì. Dall'anteprima, il pulsante « Modifica » permette di cambiare numero di colori, orientamento o inquadratura "
         "senza reinviare la foto."),
        ("Come si paga ed è sicuro?",
         "I pagamenti con carta sono gestiti da Stripe. Non conserviamo alcun dato bancario. Riceverai una fattura via e-mail."),
        ("Posso restituire il kit?",
         "Essendo realizzato dalla tua foto, il kit non è soggetto al diritto di recesso. Un prodotto difettoso o danneggiato "
         "viene sostituito o rimborsato: contattaci con una foto."),
        ("Che fine fanno le mie foto?",
         "Servono solo a realizzare la tua tela. Non vengono mai pubblicate né vendute, e puoi chiederne la cancellazione "
         "in qualsiasi momento."),
    ],
}


def faq_items(lang=None):
    lang = (lang or get_language() or "fr")[:2]
    items = FAQ.get(lang, FAQ["en"])
    c = _ctx()
    return [{"q": q, "a": a.format(**c)} for q, a in items]
