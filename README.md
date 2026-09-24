# Atelier Couleurs : boutique peinture par numéros (Django)

Interface orientée conversion : **accueil (CTA)** → **upload photo** → **aperçu généré**
→ **livraison** → **paiement Shopify**.

## Lancer
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
# http://127.0.0.1:8000/
```
Aucune base de données à migrer : les sessions sont signées côté cookie.

## Flux & routes
| Route            | Vue       | Rôle |
|------------------|-----------|------|
| `/`              | home      | landing + call-to-action |
| `/personnaliser/`| upload    | photo + format + nb couleurs |
| `/apercu/`       | preview   | rendu colorié + toile numérotée + palette + prix |
| `/livraison/`    | delivery  | adresse de livraison |
| `/commander/`    | checkout  | passage Shopify (ou récap démo) |

Le moteur d'image est `pbn.py` (à la racine), appelé par `studio/pipeline.py`.
Chaque aperçu produit un dossier `media/orders/<REF>/` (SVG + PNG) et une réf. unique.

## Shopify (3 modes, auto-détectés)
Variables d'environnement :
- `SHOPIFY_STORE` = `maboutique.myshopify.com`
- `SHOPIFY_ADMIN_TOKEN` → **Draft Order API** : crée une commande brouillon avec le
  produit personnalisé (réf., format, couleurs en *line item properties*) + adresse,
  puis redirige vers l'`invoice_url` de paiement. ← recommandé pour un produit sur-mesure
- `SHOPIFY_VARIANT_ID` (sans token) → **cart permalink** : panier pré-rempli.
- Rien de configuré → **mode démo** : page récapitulative locale.

## Réglages pipeline (env)
- `PBN_DPI` (défaut 90) : résolution de travail de l'aperçu (plus haut = plus lent).
- `PBN_BRAND` : nom de marque imprimé sur la palette.

## Prod (à prévoir)
- Générer les gros templates HD en tâche asynchrone (Celery/RQ) plutôt qu'en requête.
- Stockage média sur S3, `DEBUG=0`, `ALLOWED_HOSTS`, `DJANGO_SECRET_KEY`.

## Mises a jour recentes
- Accueil : slider avant/apres (photo vs toile numerotee), demarre a 50/50,
  glissable a la souris et au tactile. Images de demo dans
  `studio/static/studio/img/` (remplacables).
- Formats standards uniquement : A4 21x29,7 ; 30x40 ; 40x50 ; 50x70 ; 60x80 ;
  70x100 ; 80x120, avec choix Portrait/Paysage (recadrage auto au bon ratio).
- Couleurs : 12 / 24 / 36 / 48.
- Chargement : animation abstraite facon Mondrian qui se compose (pas de traits
  de construction).
- Apercu protege : images servies par une vue liee a la session (autre session
  = 404), `X-Robots-Tag: noindex`, `Cache-Control: no-store`, `robots.txt`, clic
  droit / glisser / selection desactives, SVG vectoriel non expose (rendu raster).
  Limite : une capture d'ecran reste toujours possible cote client.
- Performance : le SVG et le rendu raster partagent le meme calcul de zones, et
  l'apercu web plafonne la resolution de travail a 1100 px (`max_px`) tout en
  gardant la taille physique du SVG. Le nombre de couleurs et le format influent
  directement sur le temps de calcul.
- La requete Chrome `/.well-known/appspecific/com.chrome.devtools.json` (sonde de
  DevTools, sans rapport avec l'app) renvoie 204 pour ne pas polluer les logs.

## Dossier de production (au checkout)
Au passage a l'etape paiement, un dossier `media/orders/<REF>/` est enregistre avec :
- `<REF>_template.svg` : le trace vectoriel a peindre.
- `<REF>_poster.png` : apercu colorie en haut, palette + code de reduction en dessous.
- `order.json` : informations de commande (produit + client + line item Shopify).
- `print.json` : dimensions d'impression du SVG (taille physique) et du poster (px + mm a 150 dpi).
- `<REF>_colors.json` : liste des couleurs et de leur numero (hex + rvb).
En production, declencher `fulfillment.build()` sur la confirmation de paiement (webhook Shopify)
plutot qu'a l'affichage du checkout.

## Commande fournisseur + codes de reduction
- `studio/supplier.py` : `place_order()` envoie la spec de production au fournisseur.
  Si `SUPPLIER_API_URL` (+ `SUPPLIER_API_TOKEN`) est defini, POST reel ; sinon mode
  demo. Dans tous les cas `supplier_order.json` est ecrit dans le dossier commande.
- La vue `place_order` (POST `/commander/valider/`) declenche, dans l'ordre :
  consommation du code de reduction, assemblage du dossier, commande fournisseur,
  emission du code fidelite de la commande, puis passage Shopify (ou confirmation demo).
- Codes de reduction a usage unique (`studio/discounts.py`, store fichier
  `media/discounts.json`) : chaque commande imprime sur son poster un code (le QR
  encode `PBN_DISCOUNT_URL?id=<REF>`). Deux usages :
  - Scan du QR -> vue `redeem` (`/remise/?id=...`) : applique la remise et redirige
    vers le paiement.
  - Saisie manuelle du code a l'etape paiement.
  Le code est consomme (marque `used`) a la validation de la commande ; toute
  reutilisation est refusee. `PBN_DISCOUNT_RATE` regle le taux (0.15 par defaut).
  En production : remplacer le store fichier par une table SQL (contrainte
  d'unicite + transaction) et declencher fournisseur/consommation sur le webhook
  Shopify `orders/paid`.

## Passage en SQLite + dropshipping (MAJ)
- Base SQLite (`db.sqlite3`) : `python manage.py migrate` avant le premier lancement.
  Sessions en base, et deux modeles : `Discount` (code a usage unique, consomme par
  UPDATE atomique) et `Order` (trace des commandes passees, avec ref. fournisseur).
- Poster desormais au format **A4 portrait** (2480x3508 px, 300 dpi) : moitie haute =
  apercu colorie, moitie basse = palette (jusqu'a 36 couleurs, grille 6 colonnes) +
  petit bandeau remise fidelite (QR + code). Genere par `fulfillment._compose_poster_a4`.
- Fournisseur en full dropshipping (MOQ 1) : `supplier.place_order` envoie un payload
  avec `quantity: 1`, la liste `pots` (numero + hex a melanger, 3,5 ml), et des
  `instructions` explicites (remplir et numeroter les pots, imprimer le SVG et le
  poster A4, ajouter le logo de marque). Fournir l'asset logo au fournisseur.

## Webhook Shopify orders/paid + admin (MAJ)
Fulfilment declenche au PAIEMENT reel :
- En mode Shopify (`SHOPIFY_STORE` + `SHOPIFY_ADMIN_TOKEN`), `place_order` cree la
  commande en base au statut `pending`, cree la commande Shopify et redirige vers le
  paiement. Aucun code n'est consomme, aucune commande fournisseur n'est passee a ce stade.
- Shopify appelle ensuite le webhook `POST /webhooks/shopify/orders-paid/`. La vue verifie
  la signature HMAC (`SHOPIFY_WEBHOOK_SECRET`), retrouve la commande par sa reference (uid,
  passe en line item property `Reference`), consomme le code de reduction, assemble le
  dossier, declenche la commande fournisseur, emet le code fidelite et passe la commande a
  `fulfilled`. Rejeu idempotent, mauvaise signature -> 401.
- En mode demo (pas de Shopify configure), `place_order` fulfille immediatement, faute de
  paiement reel.

Enregistrer le webhook cote Shopify (Admin > Parametres > Notifications/Webhooks, ou via
l'API) : topic `orders/paid`, format JSON, URL `https://VOTRE_DOMAINE/webhooks/shopify/orders-paid/`,
et definir `SHOPIFY_WEBHOOK_SECRET` avec le secret fourni par Shopify.

### Admin Django
- Activer un compte : `python manage.py createsuperuser`.
- Interface sur `/admin/` : modeles `Order` (statut, produit, remise, total, ref. fournisseur)
  et `Discount` (code, statut issued/used, qui/quand) avec recherche et filtres.

## Ameliorations (MAJ)
- pbn.py, lissage : filtre de mode sur la carte de labels (supprime escaliers et
  pixels isoles, reduit les traces qui se recoupent), regle par le niveau de detail.
- pbn.py, securite paintability : `clean_small` fusionne desormais les zones trop
  petites en AIRE (`min-zone-mm`) ET trop FINES (rayon inscrit < ~0,6 mm), donc
  impossibles a peindre. Passes d'aire rapides + une passe de finesse.
- Etape 1 : prix estime affiche et recalcule en direct au changement de format,
  d'orientation ou de nombre de couleurs (constantes de prix partagees entre
  `pipeline.compute_price` et le JS via `json_script`). Plus besoin de regenerer
  a l'etape 2 pour connaitre le prix.
- Livraison : ajout d'un indicatif telephonique + numero, repris dans `order.json`,
  le payload fournisseur (`ship_to.phone`) et l'adresse Shopify.

## Difficulte, i18n, verification d'adresse, branding (MAJ)
- Curseur difficulte a l'etape 1 : Debutant / Moyen / Expert. Il regle le lissage et
  la taille minimale peignable via pbn.py (`--min-paint-mm`, rayon inscrit) et
  `min-zone-mm` : Debutant 1,5 mm, Moyen 1,0 mm, Expert 0,6 mm. Expert = plus de
  details, Debutant = zones plus grandes et simples.
- Traduction (i18n) : FR (langue source) + EN. `LocaleMiddleware` detecte la langue
  du navigateur (en-tete Accept-Language) ; un selecteur FR/EN dans l'en-tete permet
  de forcer le choix (via `set_language`, memorise en session/cookie). Catalogue
  anglais dans `locale/en/LC_MESSAGES/`. Pour ajouter/mettre a jour des traductions
  avec gettext : `makemessages -l en` puis `compilemessages` (ce projet compile aussi
  le .mo via polib faute de binaire gettext).
- Verification d'adresse (`studio/address.py`) : a la saisie (validation du
  formulaire) et de nouveau a la validation de commande. Controle des champs requis,
  du format de code postal selon le pays (FR, BE, CH, LU, DE, ES, IT, GB, CA, US) et
  du telephone. Hook optionnel vers une API externe via `ADDRESS_API_URL`.
- Branding PaintIt : nom du site et de marque = PaintIt (poster inclus, `PBN_BRAND`).
  Palette du site alignee sur le logo (navy #12224F + bleu #2F6BF2). Logo vectoriel
  ameliore dans `studio/static/studio/img/logo.svg` (icone + wordmark), utilise dans
  l'en-tete.

## E-mails (Postfix) + Contact (MAJ)
- E-mail de confirmation : envoye au client des que la commande est approuvee (payee).
  Il part depuis `_fulfill`, donc en mode demo a la validation, et en mode Shopify au
  webhook `orders/paid` (paiement reel). Templates : `studio/templates/studio/email/
  order_confirmed.txt` et `.html`. L'echec d'envoi ne bloque jamais la commande.
- Backend e-mail = SMTP via Postfix local par defaut (`EMAIL_HOST=localhost`,
  `EMAIL_PORT=25`, sans auth ni TLS). Installer Postfix en mode envoi et le laisser
  ecouter sur localhost:25. Variables : `DEFAULT_FROM_EMAIL`, `SUPPORT_EMAIL`,
  `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`. Pour tester sans Postfix :
  `DJANGO_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend`.
- Bouton Contact : lien dans l'en-tete vers `/contact/` (formulaire nom, e-mail, sujet,
  message). L'envoi va a `SUPPORT_EMAIL` avec `reply_to` = e-mail du visiteur.

## OEM, lissage, poster, avis, admin, pays (MAJ)
- Fournisseur OEM (dropshipping) : `studio/supplier.py` a trois canaux, par priorite
  API (`SUPPLIER_API_URL`), e-mail PO (`SUPPLIER_ORDER_EMAIL`), puis demo. Les usines
  OEM (VANCY ARTS, Shenzhen Yingsa, SINOART...) n'exposent pas d'API publique : le canal
  reel est le bon de commande par e-mail avec fichiers joints (SVG, poster, colors.json).
  Renseigner `SUPPLIER_NAME` et surtout `SUPPLIER_ORDER_EMAIL` avec l'adresse fournie par
  l'usine apres onboarding (tant qu'il est vide, mode demo, aucun envoi).
- Lissage : les frontieres sont arrondies (Chaikin) apres simplification -> fini l'aspect
  crenele.
- Poster A4 : inclut le logo du site en tete et est redige dans la langue de la commande
  (FR/EN), via `Order.lang`.
- Accueil : bandeau d'avis (note + temoignages). ATTENTION : contenu d'exemple a remplacer
  par de vrais avis. N'affichez une note/logo Trustpilot que si vous avez un compte
  Trustpilot reel ; les faux avis sont illegaux (UE/France).
- Admin : messages de contact stockes et repondables depuis l'admin (la reponse est
  envoyee au client). Colonnes commande enrichies (cout, marge, langue, statut). Tableau
  financier staff sur `/tableau-finances/` (CA, cout dropshipping, marge par mois,
  graphiques Chart.js), lien depuis l'accueil de l'admin. Le cout dropshipping est
  estime (`SUPPLIER_COST_*`).
- Livraison : ~30 pays (avec formats de code postal) et ~29 indicatifs telephoniques.

## Logo/favicon, auto-orientation, livraison offerte, langue poster (MAJ)
- Logo retravaille (`studio/static/studio/img/logo.svg`) + icone/favicon
  (`icon.svg`, `favicon.png`) et vignette de partage (`og:image` = logo.png) dans le head.
- Upload : l'orientation Portrait/Paysage est detectee automatiquement d'apres les
  dimensions de la photo choisie (modifiable a la main), et le prix se met a jour.
- Accueil : "Livraison offerte" en barre d'annonce (toutes pages) et en badge dans le hero.
- Poster : la langue suit le PAYS DE LIVRAISON (francophone -> FR, sinon EN par defaut),
  independamment de la langue d'interface. Logo centre, bloc remise en bas a droite.

## Refonte majeure (MAJ)
- Paiement : Shopify remplace par Stripe Checkout. `payments.py` cree une session
  hebergee ; webhook `POST /webhooks/stripe/` (checkout.session.completed) declenche le
  fulfilment. Sans `STRIPE_SECRET_KEY` -> mode demo (fulfilment immediat). Variables :
  `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`.
- Dossier commande : un SEUL `order.json` complet (produit, prix, remise, pinceaux,
  client, specs d'impression ET liste couleurs->numeros) + `_template.svg` + `_poster.png`.
  Plus de colors.json / print.json / supplier_order.json. Le fournisseur OEM lit order.json
  (API ou e-mail PO avec pieces jointes).
- Note : les commandes seulement "previsualisees" (non payees) conservent des
  intermediaires (preview/template/palette) ; seules les commandes honorees ont order.json.
- Grille tarifaire editable dans l'admin (modele `Pricing`, singleton) : prix de base,
  prix/cm2, prix/couleur, prix pinceaux, taux de remise, couts dropshipping. Tout le site
  et la marge s'y referent dynamiquement.
- Poster : logo en bas a gauche, remise en bas a droite ; langue selon le pays de livraison.
- Etape 1 : "Livraison offerte" entre le prix estime et le bouton ; auto-detection
  portrait/paysage ; format A4 retire ; ~30 pays / ~29 indicatifs.
- Apercu : bouton "Retour" qui conserve la photo (changer juste les couleurs sans re-uploader),
  et case a cocher pinceaux (+prix de la grille) avant de commander.
- Fin de commande : telechargement d'un recu PDF (`/commande/<uid>/recu.pdf`).
- Admin "tour de controle" : `/tableau-finances/` (KPIs CA/cout/marge, en attente, codes,
  messages sans reponse, graphiques, dernieres commandes). Messages de contact repondables
  depuis l'admin. Branding PaintIt (logo sans cadre + favicon + og:image).

## Ajustements UI/logo (MAJ)
- Logo : ton logo (icone + wordmark) vectorise (vtracer) en SVG propre -> logo.svg / icon.svg,
  plus logo.png (poster) et favicon.png. Wordmark quantifie navy/bleu pour des lettres nettes.
- Accueil : badge "Livraison offerte" du hero retire (la barre d'annonce reste). L'info
  reste aussi en etape 1 entre le prix et le bouton.
- URLs en anglais : /create, /preview, /preview/options, /delivery, /checkout,
  /checkout/place, /order/confirmed, /order/<uid>/receipt.pdf, /discount, /dashboard,
  /current-photo. (Les noms d'URL internes sont inchanges, aucun template casse.)
- Recu PDF : petit apercu de l'oeuvre en haut a droite + texte localise selon le pays de
  livraison (francophone -> FR, sinon EN), comme le poster.
- Tableau de bord : logo sur pastille blanche (rendu correct sur fond fonce).

## Admin "tour de controle" + grille tarifaire par format (MAJ)
- Theme admin custom (studio/static/studio/css/admin.css + templates/admin/base_site.html) :
  palette PaintIt, logo sur pastille, modules en cartes, boutons arrondis.
- Accueil admin (templates/admin/index.html) : bandeau "Tour de controle" avec KPIs live
  (CA, cout, marge, commandes, en attente) + bouton vers le suivi financier /dashboard/.
- Suivi financier /dashboard/ : graphiques + table "Ventes par format" + retour admin.
- Grille tarifaire (modele Pricing) revue : un prix par format (30x40 -> 80x120), un
  supplement par palette de couleurs (12/24/36) et le prix des pinceaux, plus remise,
  livraison et couts dropshipping. Prix du site = prix format + supplement couleurs.
  Editable dans l'admin en sections claires (Prix par format / Supplement couleurs /
  Options / Remise & livraison / Couts).

## Corrections UX + moteur (MAJ)
- Plus de colors.json qui traine : la liste des couleurs transite par la session et
  n'est ecrite que dans order.json (les dossiers d'apercu non payes ne contiennent que
  preview/template/source ; les commandes honorees ont order.json + poster).
- "contours" -> "canvas" dans les textes ; accents ajoutes sur tout le texte francais du site
  (le catalogue EN a ete resynchronise). Poster/PDF via OpenCV restent sans accents (limite
  Hershey) ; le PDF (reportlab) est deja accentue.
- Selecteur de difficulte supprime : mode dynamique unique, plancher "aucune zone < 1 mm2"
  (le nombre de zones depend donc du dessin et de la taille du canvas). ATTENTION : 1 mm2
  donne un template tres detaille (fichier SVG lourd, zones difficiles a peindre a la main).
  Le plancher se regle dans studio/pipeline.py (min_zone_mm).
- "Prix estime" -> "Prix". Loader Mondrian : sequence "Lecture de l'image > Analyse des
  couleurs > Creation du canvas > Numerotation des zones > Finalisation".
- Reference de commande retiree de l'apercu, de la livraison et du recapitulatif.
- Confirmation : bloc "Dossier de production" retire (info interne).

## Cadrage + plancher adaptatif (MAJ)
- Page creation : apercu du cadrage en direct. Des qu'une photo est choisie (ou conservee),
  un cadre montre le recadrage centre selon l'orientation/format ; il se met a jour quand on
  bascule portrait/paysage ou change de format (CSS aspect-ratio + object-fit cover, identique
  au recadrage serveur _center_crop_to_ratio).
- Plancher de zone adaptatif (studio/pipeline.py _dynamic_min_zone) : le cote mini de zone
  depend du nombre de couleurs (12 -> plus gros, 36 -> plus fin) et de la taille du canvas
  (grand -> un peu plus fin), borne entre 2.0 et 4.5 mm pour rester peignable a la main et fun.
  order.json indique le mode et la zone mini appliquee.

## Loader temps reel + nettoyages UI (MAJ)
- Generation asynchrone : /create/ lance la generation dans un thread ; la page
  /create/processing/ interroge /create/progress/ et affiche la VRAIE phase du moteur pbn
  (Lecture de l'image > Analyse des couleurs > Regroupement des zones > Trace du canvas >
  Numerotation > Finalisation), chaque message une seule fois, avec barre de progression.
  A la fin, /create/finalize/ place la commande en session et redirige vers l'apercu.
  NOTE : le suivi est en memoire de process (GEN_JOBS) -> parfait avec runserver ; en prod
  multi-worker, remplacer par un cache/broker partage (Redis) ou Celery.
- pbn.run emet la progression via un callback optionnel (args.progress) a chaque phase.
- Page creation : miniature photo retiree (l'apercu du cadrage la remplace).
- Paiement : encadre "Astuce" (code fidelite) retire.
- Admin : theme clair force (meme si l'OS est en mode sombre) + couleurs de texte des champs
  explicites -> les valeurs sont de nouveau lisibles dans la grille tarifaire.

## Deploiement production
1. Variables d'environnement : voir `.env.example` (clef secrete longue, ALLOWED_HOSTS,
   SITE_URL, Stripe, e-mail, et REDIS_URL si plusieurs workers).
2. `pip install -r requirements.txt`
3. `python manage.py collectstatic --noinput` (statiques servis par WhiteNoise, hashes+gzip).
4. `python manage.py migrate`
5. Lancer avec gunicorn en workers **gthread** (voir `Procfile`) :
   `gunicorn pbnsite.wsgi:application --worker-class gthread --workers 3 --threads 4 --timeout 120`
   Les threads permettent a la barre de progression d'etre servie pendant la generation.
6. Stripe : declarer le webhook `https://VOTRE-DOMAINE/webhooks/stripe/`
   (evenement `checkout.session.completed`) et renseigner `STRIPE_WEBHOOK_SECRET`.
7. Renseigner `SUPPLIER_ORDER_EMAIL` (ou l'API) apres onboarding OEM.

### Notes d'architecture
- Generation asynchrone : lancee dans un thread, etat (progression + resultat) stocke
  dans le cache Django. En multi-worker, **definir REDIS_URL** pour un cache partage,
  sinon la page de progression peut tomber sur un worker sans l'etat. Pour une charge
  elevee, deporter `_run_generation` dans une tache Celery (fonction deja isolee,
  basee sur le cache : migration triviale).
- Media (uploads + commandes) : monter un volume persistant et definir `MEDIA_ROOT`.
  Les fichiers ne sont jamais exposes en direct : ils passent par des vues protegees par
  session (apercu) ou restent internes (poster, order.json envoyes a l'OEM).
- Langues natives : FR, EN, DE, ES (detection navigateur + selecteur). Le poster et le
  recu PDF sont dans la langue du PAYS DE LIVRAISON (francophone -> FR, germanophone -> DE,
  hispanophone -> ES, sinon EN).

## E-mail de confirmation + UI (MAJ)
- E-mail de confirmation client : brande (bandeau PaintIt + logo servi via SITE_URL),
  mise en page en carte, recapitulatif (produit, pinceaux, remise, livraison offerte, total)
  et adresse ; localise selon le pays de livraison (FR/EN/DE/ES). Le logo est charge par URL
  (statique public) ; l'apercu de l'oeuvre n'est pas joint (media protege).
- Page creation : petit pictogramme camion devant "Livraison offerte".
- Apercu : curseur loupe au survol, et clic pour un affichage plein ecran (lightbox,
  Echap ou clic pour fermer). Les images restent protegees (clic droit/glisser desactives).

## RGPD / confidentialite (MAJ)
- Page publique /privacy/ (lien "Confidentialite" en pied de page), localisee FR/EN/DE/ES :
  les donnees (photo, commande, livraison) servent uniquement au traitement et a la livraison,
  puis sont supprimees sous un mois ; paiement via Stripe (aucune donnee de carte stockee) ;
  droits d'acces/rectification/suppression via le formulaire de contact.
- Pour que la suppression soit reelle : commande `python manage.py purge_old_data`
  (option `--days`, `--dry-run`). Elle supprime les commandes et leurs fichiers de plus de
  30 jours, les dossiers d'apercu non payes et les uploads bruts anciens.
  A PLANIFIER en cron quotidien, ex. : `0 3 * * * cd /app && python manage.py purge_old_data`.

## QR / promo / dispo / loader / numeros (MAJ)
- QR du poster : /discount/?id=CODE ne consomme plus le code, il le PRE-REMPLIT dans le
  champ code de la page paiement ; le client clique "Appliquer".
- Codes promo multi-usage : creables en admin (type "Promo", %, nombre d'utilisations ;
  0 = illimite). Compteur d'usages atomique, colonne "Restant". Les codes fidelite restent
  a usage unique. Le taux applique suit le code (ex. 50%).
- Disponibilite des offres (grille tarifaire) : cases pour activer/desactiver chaque format,
  chaque palette de couleurs et l'option pinceaux si rupture fournisseur ; le site masque
  automatiquement les offres indisponibles.
- Loader : les messages suivent la langue (FR/EN/DE/ES) via des cles de phase traduites cote
  serveur selon l'avancement reel du moteur.
- Numeros du template : dimensionnes pour tenir dans le cercle inscrit de chaque zone
  (un seul par zone) ; zones trop petites laissees sans numero -> plus de chevauchement.

## Refonte du moteur PbN (qualite a couleurs elevees) (MAJ)
Le probleme a 24/32/36 couleurs : des milliers de micro-zones "courbes de niveau".
Corrections dans pbn.py / pipeline.py :
- PLAFOND du nombre de zones (limit_zones) : on augmente le seuil d'aire et on refusionne
  jusqu'a passer sous max_zones = surface_cm2 * (0.16 + couleurs*0.004), borne [120, 1200].
  Ex. 40x50 : 36c -> ~460 zones (vs milliers), 12c -> ~270.
- Lissage plus fort quand il y a beaucoup de couleurs : meanshift + mode_filter 5x5 x3,
  et resolution de travail reduite (820-920 px) au-dela de 24/32 couleurs.
- Contours plus doux : Chaikin 3 iterations + eps augmente en mode dense.
- Detail adaptatif selon le nombre de couleurs (plus de couleurs -> plus de lissage).
- Plancher de zone releve (2.4 a 5 mm) ; numeros dimensionnes pour tenir dans la zone.
Reglages : densite de zones et max_zones dans pbn.run ; planchers dans pipeline._dynamic_min_zone.

## Cadrage manuel + logo + aperçu + admin (MAJ)
- Etape photo : le cadre d'apercu est desormais DEPLACABLE (glisser pour recentrer) ; le
  point focal (focus_x/focus_y) est transmis au recadrage serveur (_center_crop_to_ratio).
- Logo : fond blanc retire (SVG transparent) -> s'affiche sur n'importe quel fond. Sur les
  surfaces sombres (admin, tableau de bord) on utilise l'icone seule + texte blanc (le
  wordmark navy resterait invisible sur fond fonce).
- Apercu : les visuels s'affichent dans l'orientation du canvas (portrait en hauteur) avant
  le zoom plein ecran.
- Admin : cliquer sur "Grille tarifaire" ouvre DIRECTEMENT le formulaire d'edition (plus de
  sous-liste) ; l'accueil admin a un bouton "Tarifs & offres".

## Logo definitif + accueil admin "lanceur" (MAJ)
- Logo redessine a la main (SVG, fond transparent) : tuile PbN avec liseres blancs (soleil,
  montagnes, colline, eau, pinceau, numeros) + wordmark PaintIt. Net sur tout fond ; sur
  surfaces sombres, l'icone seule + texte blanc.
- Accueil admin transforme en tableau de bord d'accueil : bandeau KPIs + galerie de cartes
  cliquables (Commandes, Tarifs & offres, Codes de reduction, Messages, Suivi financier,
  Voir le site) avec compteurs en direct. Fini la liste Django brute a l'accueil.

## Corrections (logo, contact, PbN bords, PDF, admin) (MAJ)
- Logo : retour a TON logo vectorise (liseres blancs conserves), fond transparent obtenu par
  clip de l'exterieur (plus de boite blanche, coins nets sur tout fond).
- Contact : ajout de pieces jointes (images/PDF, 5 max, 8 Mo chacun) jointes a l'e-mail support.
- PbN : un peu plus de detail (plafond de zones et resolution releves, plancher abaisse) et
  numeros JAMAIS coupes aux bords (bord de l'image traite comme frontiere + position clampee).
- Recu PDF : traductions corrigees (accents FR/DE/ES via Latin-1) + orientation localisee.
- Admin : Commandes en sections avec badges de statut colores, date_hierarchy, colonnes marge ;
  Codes de reduction en sections ; pictogrammes de la galerie remplaces par des SVG.

## Logo (vraie police), apercu SVG, langue dynamique (MAJ)
- Logo : le wordmark utilise desormais une VRAIE police arrondie (Baloo 2 Bold, embarquee
  dans studio/static/studio/fonts/) -> lettres avec vrais trous (fini le "Pa" plein). Icone
  vectorisee conservee (liseres blancs, coins transparents). En-tete du site = icone SVG +
  wordmark HTML (@font-face) ; logo.png (poster/og) rasterise avec la police installee.
- Apercu : le rendu colorie ET le template sont servis en SVG vectoriel (build_preview_svg
  + template.svg) -> qualite nette a toute echelle et au zoom. Les PNG restent generes pour
  le poster / PDF / e-mail.
- Langue : le selecteur ne recharge plus la page. Le texte est echange dynamiquement cote
  client (dictionnaire studio/static/studio/i18n.js genere depuis les catalogues) et un
  cookie de langue est pose pour que la navigation suivante reste dans la langue choisie.

## Corrections (i18n dynamique, PDF/e-mail, delai, densite, admin) (MAJ)
- Bascule de langue dynamique : passage a un remplacement par SOUS-CHAINE (trie par
  longueur) -> topbar, etapes (1. Photo / 2. Apercu...), option pinceaux et recapitulatif
  se traduisent maintenant sans rechargement.
- Recu PDF + e-mail de confirmation : localises selon la LANGUE DU PROCESS d'achat
  (get_language capturee au paiement, stockee sur la commande), plus selon le pays. Le
  poster reste dans la langue du pays de livraison.
- Delai de livraison : reglable en admin (Grille tarifaire -> delivery_days_min/max) et
  affiche (localise) avant le bouton "Valider et payer".
- Densite PBN : plancher abaisse + plafond de zones releve -> plus de granularite
  (ex. ~730 zones a 36 couleurs en 40x50). Reglages : density dans pbn.run, base dans
  pipeline._dynamic_min_zone.
- Bug admin corrige ('OrderAdmin object has no attribute format' : format_html etait
  importe comme attribut de classe et se liait a self).

## Granularite, suivi colis, e-mail d'avis (MAJ)
- Densite PBN "etat de l'art" : resolution de travail adaptee a la taille du canvas
  (1100->1400 px), plancher de zone abaisse (~1.4-2.3 mm) et plafond releve
  -> ~800 zones sur 40x50/36c, ~1200-1300 sur grands formats. Cout : generation ~10-18 s
  (couverte par la barre de progression). Reglages : pipeline._dynamic_min_zone + max_px,
  density dans pbn.run.
- Accents corriges sur la ligne "Livraison estimee : X a Y jours ouvres".
- Suivi colis en admin : champs Transporteur / N0 de suivi / Lien de suivi, statuts
  Expediee et Livree, colonne + badge de statut, filtre par transporteur, et actions
  "Marquer expediee", "Marquer livree + demander un avis", "Envoyer la demande d'avis".
- E-mail d'avis automatique : envoye (une seule fois, drapeau feedback_sent) quand une
  commande passe a "Livree" (via save ou action). Localise selon la langue de la commande,
  brande, avec bouton vers REVIEW_URL (variable d'env) si defini, sinon invitation a repondre.

## Admin "control tower" : refonte des tables (MAJ)
- Toutes les listes admin sont restylees (studio/static/studio/css/admin.css) : tableau en
  carte arrondie avec ombre, en-tetes en petites capitales gris-bleu, lignes aerees + survol,
  selection en surbrillance, liens en bleu marque. Recherche, barre d'actions, filtres
  lateraux (options en pilules, filtre actif surligne) et pagination (boutons arrondis)
  redessines. Formulaires : barre d'enregistrement collee en bas, champs et sections en cartes.
  Cible les selecteurs standard Django (#result_list, #changelist-filter, .paginator,
  #toolbar, .actions, .submit-row) : s'applique a Order, Discount, ContactMessage, Pricing.

## Simplification mono-produit + DigiPaint (MAJ)
- Parcours simplifie (pseudo mono-produit) : la page de creation ne demande plus que la
  PHOTO + portrait/paysage + recadrage. Le format est fixe (40 x 50) et le nombre de
  couleurs par defaut (24) ; le prix affiche est fixe. Generation en tache de fond (barre).
- DigiPaint (gamification) : bouton sur l'apercu -> /paint/<uid>/. Jeu plein ecran qui
  charge un SVG jouable (nouveau build_digipaint_svg : une zone = un <path> cliquable avec
  data-n = numero cible et data-a = surface). Palette numerotee a droite + modele a
  reproduire. On choisit une couleur/numero et on peint la zone correspondante en cliquant ;
  plus la zone est petite, plus elle rapporte de points. Zoom molette + glisser, mode
  "Libre" (open color), barre de progression, score, plein ecran, confettis a 100%,
  sauvegarde locale (localStorage) par toile.
- A VENIR (bases posees) : mode collaboratif/battle (necessite un backend temps reel /
  websockets) et synchro "MyPaint" sur compte client (necessite l'authentification client).
  Le solo est complet et jouable ; l'ajout du multi/compte est une extension.

## Couleurs physiques, jeu jusqu'a 99 couleurs, sauvegarde des modeles (MAJ)
- Boutique physique : le choix 12 / 24 / 36 couleurs est retabli (format fixe 40x50, prix
  dynamique). La simplification "un seul format" est conservee.
- DigiPaint : curseur "Detail du jeu" 12 -> 99 couleurs. "Regenerer le jeu" relance une
  generation DEDIEE AU JEU (a partir de la source deja cadree), sans toucher a la commande
  physique. Plateau + palette recharges dynamiquement. Plus de couleurs = plus fin (numerique,
  sans contrainte de peinture) mais generation plus longue.
- Mes creations : page /my-models/ (galerie locale, localStorage) pour rejouer en DigiPaint
  ou re-commander plus tard. Reprise possible sans session via le lien du modele (uid non
  devinable) ; l'endpoint /restore/ reconstitue la commande. colors.json est desormais
  conserve (necessaire a la reprise et a la palette du jeu).
  NOTE : sauvegarde par appareil (localStorage), pas par compte ; l'acces "plus tard" est
  limite par la purge RGPD (30 j) des fichiers. Une vraie sauvegarde multi-appareils
  necessitera des comptes clients (auth).

## Galerie par e-mail + reprise par code + commande depuis le jeu (MAJ)
- Mes creations : champ e-mail -> "M'envoyer par e-mail" envoie la galerie (codes uid +
  liens Jouer + vignettes) via /my-models/email/ (POST email + models du localStorage).
  Permet de reprendre sur un autre appareil grace aux codes. E-mail brande + localise.
- Reprise par code : champ "J'ai un code" (Mes creations) et champ "Ouvrir un autre code"
  (dans le jeu) -> /paint/<code>/ ouvre directement le design DigiPaint.
- Commande depuis le jeu : bouton "Commander en reel" (form -> /restore/) qui reconstitue
  la commande a partir du code (dimensions lues dans le SVG, couleurs = palette) et mene a
  l'apercu/livraison.
  NOTE : codes/liens valides tant que les fichiers existent (purge RGPD 30 j). La galerie
  reste locale (localStorage) ; l'e-mail sert de pont entre appareils.

## Fixes jeu + minimap + jeu direct (MAJ)
- Bug corrige : la palette etait double-encodee (json_script sur une chaine deja JSON) ->
  la vue passe desormais la LISTE brute a json_script (plus d'erreur pal.forEach).
- Minimap : apercu de l'image finale en surimpression du plateau (coin bas-droite),
  cliquable pour l'agrandir/reduire, mise a jour aussi apres regeneration du jeu.
- "Aha moment" : apres la generation, on arrive DIRECTEMENT dans DigiPaint (le jeu remplace
  l'ancienne page apercu). Le modele est sauvegarde automatiquement dans Mes creations, et
  le CTA ludique "Commander en reel" mene a la commande. La page apercu classique reste
  accessible via la croix ou via "Commander en reel".

## Plateau visible + parcours jeu/commande (MAJ)
- Plateau du jeu invisible : le SVG jouable n'avait pas de dimensions intrinseques -> ajout
  de width/height (mm) + fond blanc. Le canvas s'affiche desormais sur le fond bleu du jeu.
- Boutons du jeu :
  * "🏠 Recevoir a la maison" -> retour au parcours classique (/paint/<uid>/order/) : la
    photo est conservee et on repart sur la page de creation pour RECADRER / changer
    l'orientation / le nombre de couleurs, puis regenerer. Dans ce mode (buy_mode), la
    generation se termine sur l'APERCU classique (option pinceaux + "Je commande").
  * "💾 Sauvegarder pour plus tard" -> saisie e-mail + envoi du code (lien Jouer) par e-mail.
  * "Ouvrir un autre code" -> reprendre un design via son code.
- Flux : par defaut, apres generation on arrive dans le jeu (aha moment) ; via "Recevoir a
  la maison" on passe par l'apercu/commande classique.

## DigiPaint : refonte ergonomie (MAJ)
- Numeros dans les cases : le SVG jouable inclut desormais le numero cible dans chaque zone
  (dimensionne pour tenir, fonce + halo blanc). Bouton "👁 Numeros" pour les masquer/afficher.
  Ils sont fins en vue d'ensemble et deviennent lisibles au zoom.
- Rendu final : plus de doublon. Un seul apercu (minimap en bas a droite) ; clic dessus ->
  grande popup plein ecran pour analyser l'image.
- Palette en haut : barre horizontale des couleurs numerotees en haut de l'ecran, avec les
  options (Numeros/Libre, masquer numeros, "Recevoir a la maison", menu save/code).
- Navigation fluide : zoom molette centre sur le curseur, pincement 2 doigts (tactile),
  boutons zoom +/- et "ajuster", panoramique au glisser. Le clic simple peint la zone.

## DigiPaint : navigation reparee (MAJ)
- Les boutons +/-/ajuster (bas-gauche) et la minimap etaient "voles" par la capture de
  pointeur du plateau -> le plateau ignore desormais ces controles (ils sont cliquables).
- Pan (glisser 1 doigt/souris) et pincement (2 doigts) fonctionnent ; zoom molette centre
  sur le curseur.
- Pilotage clavier ajoute : fleches = deplacer, + / - = zoom, 0 = ajuster.

## DigiPaint : ouverture par l'apercu + mode libre spectre + clavier + sortie commande (MAJ)
- Plus de bouton DigiPaint : apres generation on voit l'APERCU (revelation), et cliquer sur
  l'image ouvre le jeu interactif.
- Mode "Libre" : selecteur de couleur plein spectre (input color) -> on peint avec n'importe
  quelle couleur. Mode "Numeros" = matching classique.
- Selection couleur au clavier numerique : taper le numero (ex. 2 puis 3 = 23) selectionne la
  couleur (buffer 900 ms).
- Minimap (bas-droite) : clic -> grande popup plein ecran.
- Clic HORS du dessin (fond, pas un element UI, pas une zone) -> retour a la commande
  (apercu) SANS regeneration (route /paint/<uid>/buy/ qui reconstitue la commande).

## Jeu = recompense d'achat + refonte (MAJ)
- Apercu restaure : image (rendu colorie) + toile numerotee, clic -> plein ecran (lightbox).
  Plus de bouton/jeu sur l'apercu.
- DigiPaint devient la RECOMPENSE : call-to-action sur la page de confirmation
  ("Jouer maintenant") et acces permanent via Mes creations (code/cookie).
- Traits noirs fins : le plateau superpose desormais le template (traits fins noirs + numeros)
  sur des zones remplissables -> plus de double trait.
- Combos : peindre plusieurs zones du MEME numero d'affilee augmente un multiplicateur
  (jusqu'a x5), affiche a l'ecran ("x2 COMBO").
- Celebration : confetti + badge "Oeuvre certifiee" avec effet brillant (shiny sweep).
- Setup une fois : dans le menu du jeu, curseur couleurs 12->99 + difficulte
  (Facile/Moyen/Difficile/Extreme) ; "Regenerer la toile de jeu" applique le reglage.
  NOTE : sans comptes clients, le "reserve aux joueurs en ligne / 1 m2" n'est pas verrouille
  (tout le monde en ligne y a acces) ; le vrai gating + dimensions jusqu'a 1 m2 demanderont
  l'authentification client.

## Prix a l'apercu, pots inclus, reward SVG, vignette->jeu, clavier dizaines, save (MAJ)
- Prix : retire de l'etape photo, affiche a l'apercu apres generation.
- Apercu : mention "<N> pots de peinture inclus dans l'achat" (N = nb de couleurs) + livraison offerte.
- Confirmation : bloc recompense redessine avec icones SVG (palette + play), sans emoji.
- Mes creations : cliquer sur la VIGNETTE ouvre le jeu (bouton "Jouer" supprime).
- Jeu clavier : correction des dizaines (50, 60...) -> le "0" alimente le numero au lieu de
  declencher "ajuster" ; l'ajustement passe sur la touche "f".
- Sauvegarde : le canvas configure (couleurs/difficulte) est memorise (PIT_GAME) et rechargle
  a l'ouverture -> la progression est conservee entre les sessions sur l'appareil.
  NOTE : la variante configuree est locale (localStorage) ; l'ouverture par code sur un AUTRE
  appareil recharge la toile de base (le mapping variante n'est pas encore serveur).

## Portail /paint/ (galerie gamer) + monetisation (MAJ)
- /paint/ devient le PORTAIL des joueurs : galerie des toiles, bouton de creation, et
  restauration de la galerie par CODE e-mail (envoi d'un code a 6 chiffres, verification ->
  la galerie serveur liee a l'e-mail se charge sur n'importe quel appareil).
- Monetisation : 1re toile numerique GRATUITE, puis 0,99 EUR par toile supplementaire pour
  agrandir la galerie (gate /paint/new/ -> /paint/pay/). Paiement en DEMO (credit immediat) ;
  a brancher sur un Stripe Checkout 0,99 EUR pour la prod.
- Modele : DigitalCanvas (galerie par e-mail) + EmailCode (verification). Une toile creee en
  mode digital est rattachee a l'e-mail verifie ; un achat physique enregistre en plus une
  toile "cadeau" pour l'e-mail de l'acheteur.
- Le "cadeau de fin" (confirmation) et le lien de nav "Ma galerie" pointent vers /paint/.
  Le bouton ✕ du jeu revient au portail.
  NOTE : sans e-mail verifie, la galerie reste locale (appareil) et la gratuite est comptee
  par session ; le vrai gating multi-appareils repose sur la verification e-mail.

## Portail : suppression, carte d'ajout ronde, shiny, commande depuis galerie + jeu design (MAJ)
- Suppression : bouton corbeille sur chaque carte -> supprime la toile de la galerie ET du
  backend (DigitalCanvas) ; les fichiers sont effaces si aucune commande physique n'y est liee.
  Aussi supprimable depuis l'admin (DigitalCanvas enregistre + action "supprimer avec fichiers").
- "Creer un design" : bouton rond (+) place dans la grille, a cote des designs ; libelle
  "Creer · gratuit" puis "Ajouter · 0,99 EUR".
- Shiny : une carte devient brillante (bordure doree + balayage + ✨) quand la toile est 100%
  terminee (marquee a la victoire).
- Commande depuis la galerie : chaque carte propose "Kit" (toile a peindre) et "Peinte"
  (version peinte) -> parcours de commande physique (variante memorisee).
- Jeu : fond marbre blanc epure + nuage de points ; Ctrl + fleche gauche/droite = couleur
  precedente/suivante ; bouton "?" avec regles + tous les raccourcis.
  NOTE : l'impression de la version PEINTE (produit "Peinte") n'est pas encore branchee cote
  fabrication (necessite l'export de la toile peinte en image) ; le bouton mene au parcours
  de commande avec la variante memorisee.

## Zero emoji (SVG), achat -> galerie auto, barre de jeu en anglais (MAJ)
- Plus aucun emoji : remplaces par des icones SVG integrees (galerie : Kit=panier, Peinte=cadre,
  Supprimer=corbeille, Jouer=triangle, offerte=cadeau, shiny=etoile ; jeu : cadenas, #, engrenage,
  aide, plein ecran, redemarrer, fermer ; confirmation : palette + play).
- Achat : apres paiement, la session est liee a l'e-mail de l'acheteur et le design apparait
  AUTOMATIQUEMENT dans la galerie /paint/ (toile "cadeau").
- Barre du jeu (design + anglais) : tout en anglais, barre scrollable, chiffres plus visibles
  (palette + numeros du plateau). Le toggle "numeros" devient une icone "#"/"#" barre. Le choix
  Numeros/Libre devient un seul bouton CADENAS (ferme = couleurs verrouillees aux numeros,
  ouvert = couleur libre). Ctrl + fleche gauche/droite = couleur precedente/suivante.

## Jeu : combo continu, fermeture par X, aide calligraphique, minimap, libelles galerie (MAJ)
- Combo : desormais une SERIE continue -> il monte a chaque zone correcte enchainee (jusqu'a
  x6) et ne retombe que sur une erreur.
- Fermeture : un clic sur le fond ne ferme plus rien ; seule la croix "X" quitte le jeu.
- Aide : affichee directement sur le fond (en bas a gauche, au-dessus des boutons de zoom),
  style calligraphie (serif italique) ; le bouton "?" la masque/affiche.
- Minimap : apercu final agrandi par defaut (184 px).
- Galerie : les deux boutons vers la vente physique s'appellent "Kit toile vierge" et
  "Tableau fini".

## 1re generation = toile numerique gratuite (MAJ)
- Toute generation depuis le menu de creation enregistre le design comme la 1re toile
  numerique GRATUITE de l'utilisateur (DigitalCanvas) et marque free_used. Les creations
  suivantes passent par la gate du portail (0,99 EUR) ou arrivent en cadeau d'achat.
  Pour un utilisateur dont l'e-mail est verifie, la toile est directement liee a son compte
  et visible dans /paint/ ; sinon elle apparait via la galerie locale (appareil).

## Palette hover, aide repositionnee, produit Tableau fini, homepage (MAJ)
- Palette : surbrillance au survol (agrandissement + halo).
- Aide du jeu : remontee au-dessus des boutons +/- (ne les masque plus).
- Tableau fini = PRODUIT SEPARE (impression d'art) avec son propre menu d'achat : dimensions
  (plus de formats), matiere (toile/alu/acrylique), cadre (sans/noir/bois/blanc), sous verre.
  Prix dynamique, commande envoyee a un fournisseur IMPRESSION distinct (PrintPricing.supplier_email).
  Tout est configurable dans l'admin (Tarifs Tableau fini) : prix, disponibilites, fournisseur.
  Le bouton "Tableau fini" de la galerie pointe vers /print/<uid>/.
- Homepage : faux avis supprimes ; l'etape 3 devient "On livre le kit + votre toile numerique"
  (toile digitale a jouer, offerte).
  NOTE : l'impression utilise le rendu final du design ; imprimer la version REELLEMENT peinte
  necessitera l'export de la toile peinte. Le paiement du Tableau fini est en demo (email au
  fournisseur) ; a brancher sur Stripe pour la prod.

## Homepage i18n + bandeau + CTA algo, sauvegarde mode libre, KPI centres (MAJ)
- Homepage : le titre hero est traduit (FR/EN/DE/ES) ; bandeau du haut precise "Toile
  digitale offerte" ; nouveau bouton CTA "Tester notre algorithme".
- Jeu : la peinture en mode LIBRE est desormais sauvegardee (progression conservee au
  rechargement) ; la zone Score / combo / Zones est centree dans la barre du haut.

## Fixes (crash poster, prix Tableau fini) + alignement + Tableau fini peint (MAJ)
- BUG corrige : crash a la commande physique quand la palette etait vide (rows=0 dans le
  poster) -> garde rows>=1.
- BUG corrige : "Total : --" du Tableau fini (cfg etait double-encode) -> passe la config brute
  a json_script ; le prix se calcule.
- Alignement fill/trait : le plateau du jeu est de nouveau UN SEUL SVG (remplissage + son
  propre trait noir fin + numero, meme geometrie) -> plus de decalage entre la couleur et le
  trait. Trait 0,18 mm noir.
- Homepage : titre hero en chaine traduisible unique (traduit FR/EN/DE/ES) ; le bouton devient
  "Tester notre algorithme" (remplace "Personnaliser") ; l'etape 3 montre DEUX chemins
  paralleles (kit physique + toile digitale offerte / toile digitale gratuite la 1re, sauvegarde
  par e-mail). Apercu du cadrage centre en creation.
- Tableau fini : affiche l'ETAT PEINT courant (charge le SVG jouable + applique la sauvegarde),
  calcule le prix, et permet la commande avec les MEMES champs que le kit (tel + complement
  d'adresse). L'artwork peint est enregistre (SVG) et joint au fournisseur impression.
  NOTE : l'etat peint vient du localStorage (appareil) ; pour l'impression prod il faudra
  rasteriser le SVG cote serveur en haute def. Paiement Tableau fini toujours en demo.

## Offre digitale sur l'apercu + navigation minimap (MAJ)
- Apercu : sous l'offre physique, un bloc "Jouez votre toile en digital" -> offert si c'est
  la premiere toile, sinon 0,99 EUR (route /paint/<uid>/unlock/ qui debloque puis ouvre le jeu ;
  demo -> immediat, a brancher sur Stripe).
- Jeu : cliquer sur la minimap (bas-droite) TELEPORTE la vue du canvas a cet endroit
  (centre + zoom), pour naviguer vite dans les grandes toiles.

## Fix : palette/options perdues au retour depuis la galerie (MAJ)
- Cause : si une VARIANTE de jeu configuree (PIT_GAME) n'etait plus disponible sur le serveur,
  le chargement echouait -> ni plateau ni palette.
- Correctif : loadBoard a un repli robuste -> si la variante echoue, on recharge la toile de
  BASE avec sa palette serveur (toujours fournie via colors.json, + repli order.json).

## Traduction dynamique du titre/etape 3 + note temps de generation (MAJ)
- Fix : le titre hero et l'etape 3 ne basculaient pas dynamiquement car ils n'etaient pas dans
  i18n.js (non regenere). i18n.js regenere (toutes les chaines presentes).
- Le swap dynamique devient HYBRIDE : match EXACT du noeud de texte (pour les libelles courts
  comme "ou", "Kit", "Pays" -> plus de corruption de mots type "vous"/"paysage") + remplacement
  par sous-chaine uniquement pour les cles longues (>=8, pour les bandeaux concatenes).
- Creation : note "La generation peut prendre jusqu'a une minute — plus de couleurs = plus de
  detail (et un peu plus long)" (traduite FR/EN/DE/ES).

## DigiPaint tab, KPI cartes, kit inclus, poster confirmation, print peint (MAJ)
- Onglet nav : "Ma galerie" -> "DigiPaint".
- Cartes galerie : KPI score + zones peintes (lus dans la sauvegarde locale).
- Apercu (Je commande) : visuel "Inclus dans le kit" (pots de peinture, pinceaux, emballage).
- Confirmation : affiche le POSTER recu dans le colis (order_image .../poster/) + le jeu.
- Tableau fini : artwork = etat peint courant si peint, sinon rendu couleur (fini le trace
  fade) ; champs de livraison complets ; options pilotees par l'admin (PrintPricing).

## Numeros verts (zones peintes) + teleport en 2 temps (MAJ)
- Les numeros des zones deja peintes passent en VERT (classe .cell.done sur le groupe).
- Teleport : cliquer sur la minimap DEPLIE la grande carte au centre ; cliquer sur cette
  carte te teleporte a l'endroit vise (et referme la carte).

## Aide plus explicite + filtre "zones a peindre" (MAJ)
- Aide "How to play" reecrite, plus explicite : touches clavier en <kbd>, icone de fleches,
  "F pour centrer", saisie du numero pour choisir la couleur, numero qui passe en vert.
- Nouveau bouton (a cote du #) : "focus zones a peindre" -> estompe les zones deja peintes et
  souligne celles qui restent, pour aider a finir la toile.

## Clic droit desactive, textes home, plus de tiret cadratin (MAJ)
- DigiPaint : clic droit (menu contextuel) desactive.
- Home : sous-titre remplace par "Envoyez une photo, on la transforme dans nos ateliers !" ;
  etape 2 precise "calcules automatiquement en moins d'une minute".
- Plus aucun tiret cadratin "—" dans le code/textes (remplaces par virgule / point / puce).

## Aide (position/traduction), formulaire Tableau fini, galerie sur e-mail (MAJ)
- Aide du jeu : passee en position FIXE au-dessus de tout (z-index eleve) -> plus cachee sous
  la palette ; et TRADUITE dans la langue de la commande/creation (avec Score, Zones, carte de
  victoire). Bouton "?" pour l'afficher/masquer.
- Tableau fini : formulaire de droite corrige (plus de debordement) : colonnes min-width:0,
  champs pleine largeur, bouton "Valider la commande" sur sa propre ligne.
- Galerie : plus AUCUN projet affiche tant que l'utilisateur n'a pas saisi son e-mail. Un
  simple champ "Voir ma galerie" lie la galerie a l'e-mail (verification reelle prevue en
  production) et rattache les toiles creees pendant la session. Cartes appareil retirees.

## GEO (llm.txt/json), grille tarifaire 2 colonnes, loader anime, tableau = etat peint (MAJ)
- GEO : /llm.txt et /llm.json a la racine (FAQ + mise en avant : meilleur algorithme du marche,
  en moins d'une minute, gratuit).
- Admin : nouvelle "Grille tarifaire" a /admin-tarifs/ (liee depuis l'accueil admin) : DEUX
  colonnes Kit / Tableau fini pour choisir la disponibilite ET le prix de chaque option.
  L'enregistrement impacte bien l'interface (verifie : desactiver une couleur la masque a la
  creation ; desactiver une dimension la masque sur le Tableau fini).
  (Rappel : le Kit est mono-format 40x50 ; les anciennes bascules de format n'avaient donc pas
  d'effet, d'ou la confusion , la nouvelle grille se concentre sur 40x50 + couleurs.)
- Loader : logo PaintIt + slider avant/apres auto-anime pendant la generation, pour divertir.
- Tableau fini : reflete l'etat actuel du remplissage de l'utilisateur (deja en place).

## Loader (logo + poster) + GEO + marketing (MAJ)
- Loader : le Mondrian et le POSTER (echantillon) sont cote a cote ; le slider "peint" le LOGO
  (icone en niveaux de gris -> couleur) au lieu des images de demo.
- Voir aussi paintit_promo.html (contenu marketing anime, autonome) et la liste des communautes
  UE (Reddit r/paintbynumbers, groupes Facebook, forums arts&crafts UK, forums Malen-nach-Zahlen DE).

## Aide compacte + loader simple (MAJ)
- Aide "Comment jouer" : refaite en carte compacte SOMBRE, masquee par defaut (ouverte au clic
  sur "?"), sans ascenseur ni style calligraphie -> ne recouvre plus le plateau.
- Loader : revenu au simple Mondrian + la phrase (logo/poster/slider retires).

## Jeu (palette/pots, score, min 2, no-select), promo slider, confidentialite, bibliotheque (MAJ)
- Jeu : selection de texte desactivee (les numeros ne deviennent plus bleus / ne bloquent plus
  le clic) ; aide masquee par defaut (bouton "?") ; palette DEPORTEE en petits pots compacts
  au-dessus du modele (bas-droite) ; zoom de teleportation reduit ; Score/Zones en pastilles
  blanches plus grandes et lisibles ; le curseur du generateur va de 2 a 99 couleurs.
- Promo : paintit_promo.html = slider style homepage (leur PHOTO vs la toile PBN coloriee avec
  traits), balayage auto.
- Confidentialite : precise que les fichiers d'une toile sont conserves TANT QUE le modele est
  dans la galerie (supprime de la galerie => fichiers effaces).
- Bibliotheque DigiPaint : modeles embarques CATEGORISES (Paysage, Vehicules...) affiches sur
  /paint/, jouables gratuitement. Images sources dans studio/static/studio/gallery/ (galerie + assets du home).
  IMPORTANT : lancer `python manage.py seed_library` apres installation pour generer ces modeles
  (les fichiers media ne sont pas inclus dans le zip). Ajoutez des images a la commande pour
  plus de categories.

## Palette matrice, aide iconique, slider apercu, paywall 2 min, promo zoom, confidentialite (MAJ)
- Palette du jeu : remise EN HAUT, en matrice de ronds de couleur (toutes visibles), cliquable
  (le clic etait casse quand elle etait dans le plateau ; corrige).
- Nav : "DigiPaint" -> "Galerie".
- Aide : refaite avec ICONES (touches clavier, souris, tactile, carte), plus aeree ; masquee
  par defaut (bouton "?").
- Apercu (fin de creation) : slider de revelation (leur PHOTO vs la toile PBN coloriee+traits),
  glissable, au-dessus des visuels.
- Paywall : une toile non possedee se joue 2 minutes puis affiche "Pour continuer" (0,99 EUR,
  demo) pour continuer ET sauvegarder dans la Galerie. Les toiles offertes (1re gratuite),
  cadeaux d'achat et modeles de bibliotheque sont "possedes" -> pas de paywall.
- Promo : nouveau montage qui ZOOME sur le detail quasi-fractal (zones minuscules) + carte
  entierement CLIQUABLE (lien vers le site , remplacer l'URL placeholder par votre domaine).
- Confidentialite : reecrite, structuree (sections) et traduite FR/EN/DE/ES.

## Tableau fini realiste, agrandissement, zoom F, store bibliotheque (MAJ)
- Tableau fini : matieres TOILE / ALU / BOIS (fini l'acrylique) ; prix credibles (toile
  29->119 EUR selon dimension, +alu 20, +bois 15, cadres 19-24, verre 12). Cadre et sous verre
  UNIQUEMENT pour la toile (masques et ignores pour alu/bois).
- Tableau fini : l'apercu du tableau est cliquable -> agrandissement plein ecran (lightbox).
- Jeu : touche F cycle le zoom (ajuster <-> 2x). Le zoom (molette/pincement) est desactive
  quand la carte/modele est ouverte en plein ecran.
- Store / bibliotheque : action admin "Ajouter au store (bibliotheque)" sur DigitalCanvas pour
  publier n'importe quel design genere comme modele pret a l'emploi (renseigner titre +
  categorie, editables dans la liste admin). Ils apparaissent dans la section "Bibliotheque"
  du portail /paint/. Lancer `python manage.py seed_library` pour les 3 exemples fournis.

## Point rouge minimap, galerie<->variante, nom source, store 6 categories, promos zoom (MAJ)
- Jeu : point ROUGE sur la minimap indiquant la position/vue du joueur sur la carte (se met a
  jour au pan/zoom).
- Galerie : quand on regenere une toile (params DigiPaint), la miniature ET le KPI de la carte
  refletent la variante (le portail charge l'apercu de la variante configuree).
- Fichiers : l'image originale est sauvee sous <uid>_source_<nom>.jpg (au lieu de source.jpg) ;
  tous les lecteurs utilisent un helper pipeline.source_file() (retro-compatible avec source.jpg).
- Store bibliotheque : seed_library couvre 6 modeles categorises (Fleurs: rose, cerisiers ;
  Paysage: foret ; Animaux: aquarium, beagle ; Vehicules: moto). Lancer `python manage.py seed_library`.
- Marketing : variantes de promo (dossier promos/) qui ZOOMENT en profondeur sur le SVG
  NUMEROTE (zones minuscules visibles), cliquables (remplacer l'URL par votre domaine).

## Parrainage + promo jeu (MAJ)
- Parrainage : le code du poster (QR physique) donne 40% (referral_physical_rate) ; un code de
  parrainage digital a partager donne 20% (referral_rate). Les deux taux sont editables en admin
  (Grille tarifaire / PricingAdmin). La confirmation affiche le code de parrainage a partager.
- Promo : promos/promo_jeu.html = maquette animee du JEU DigiPaint (score qui monte, combos,
  peinture des zones, palette) pour promouvoir le produit numerique.

## Home alternance images + CTA cartes galerie (MAJ)
- Home : le comparateur photo/toile alterne moto <-> autres designs (rose, aquarium, chien,
  foret, cerisier) en rotation auto (assets dans static/studio/showcase/).
- Cartes galerie : hierarchie CTA revue -> "Kit toile vierge" (bouton primaire bleu) et
  "Tableau fini" (bouton secondaire) en pleine largeur ; la corbeille devient une petite pastille
  discrete en coin de la vignette (plus de bouton aussi gros que les CTA).
- Apercu : slider photo/toile corrige (script en DOMContentLoaded + clip-path aligne).
- Kit : "Inclus dans le kit" -> Pots, Poster & instructions, Livraison, Jeu DigiPaint, Coupon.

## Details bois du jeu + responsive mobile/tablette (MAJ)
- Palette : pastilles avec gros bord type "pot" (bord bois epais + profondeur) ; zone de palette
  transparente.
- Jeu en theme BOIS : barre du haut, boutons, segments, pastilles Score/Zones, boutons zoom
  (+/-) et bordure de la minimap passes en tons bois.
- Responsive : bloc @media pour le site (nav qui s'adapte, grilles 1-2 colonnes, formulaires
  pleine largeur, anti-zoom iOS) ET pour le jeu (chrome compact, palette/minimap/zoom tactiles).
- Fun mobile : vibration (navigator.vibrate) a chaque zone correcte et a la victoire.

## Fix aperçu (erreur JS + slider) + i18n showcase + centrage (MAJ)
- Aperçu : suppression du code mort (ancien lightbox supprime) qui provoquait l'erreur console
  "Cannot read properties of null" ; slider photo/toile fiabilise (pointer capture) et il glisse ;
  knob refait (double chevron propre).
- Home : section "Des modeles prets a peindre" desormais traduite (mo recompiles + i18n.js
  regenere pour le swap client) ; sous-titre recentre.

## Fix traductions kit (cache i18n.js) (MAJ)
- Les libellés "Inclus dans le kit" (pots, poster, livraison, jeu, coupon) etaient bien traduits
  cote serveur ET dans i18n.js, mais le navigateur servait un i18n.js EN CACHE (ancien).
- Ajout d'un cache-busting : i18n.js est charge avec ?v=<hash> (mtime+taille) via un context
  processor (studio.context.assets), donc toute mise a jour du dico est rechargee automatiquement.

## Palette sur pad blanc + toile sur fond blanc (MAJ)
- Palette : pastilles-pots posees sur un PAD BLANC arrondi (ombre douce) au lieu du fond
  transparent.
- Zone de peinture : la toile flotte sur un fond blanc casse avec une ombre projetee marquee.
- Le cadre/menu (barre du haut, boutons, Score/Zones, zoom, bordure minimap) reste en bois.

## FIX slider aperçu (vraie cause) (MAJ)
- Le bloc {% block title %} n'etait pas ferme : les <script> (dont le drag du slider) se
  retrouvaient DANS <title> et ne s'executaient jamais. Titre ferme + scripts deplaces dans le
  bloc content -> le slider photo/toile fonctionne enfin.

## Apercu = slider home + toile vide + miniatures / jeu theme epure (MAJ)
- Apercu : slider IDENTIQUE a la home (composant .reveal), comparant la PHOTO et la toile VIDE
  numerotee (SVG template, plus la version coloriee). Sous le slider, 2 miniatures cliquables
  (Toile coloriee / Toile numerotee) qui s'ouvrent en plein ecran (lightbox).
- Jeu : theme marron/bois remplace par un theme EPURE ardoise/charbon (surfaces sombres
  elegantes) avec toile et palette sur blanc ; pastilles-pots a bord neutre clair.

## Toggle numeros (tableau), format unique, bloc jeu refait (MAJ)
- Tableau fini : nouvelle option "Imprimer les numeros sur le tableau" (toggle avec/sans numeros ;
  sans par defaut = rendu propre). Le choix est repercute dans le fichier envoye a l'imprimeur.
- Recap d'achat : le format n'apparait plus en double (on garde "40 x 50 cm (paysage)", on retire
  la repetition des dimensions).
- Confirmation : bloc recompense refait, plus propre -> miniature de la toile + badge "Offert" +
  GROS CTA "Jouer maintenant" qui mene directement au jeu de cette toile.

## 3 pots libres + nav dedupliquee (MAJ)
- Jeu : 3 pots "libres" remplissables ajoutes a la palette. Clic sur un pot vide -> choix d'une
  couleur -> il se remplit et devient selectionnable en mode libre (cadenas ouvert). Reclic sur un
  pot deja rempli/selectionne -> re-choix de la couleur. Les 3 couleurs sont memorisees
  (localStorage PIT_CUSTOM_POTS) et reutilisables sur toutes les toiles.
- Nav : "Modeles" et "Galerie" faisaient doublon -> on ne garde que "Galerie" (le store est dedans).

## Onglets Gallery / DigiPaint(TM) + palette en tiroir (MAJ)
- Nav clarifiee : "Gallery" (/gallery/ = tous les modeles PaintIt, certains gratuits, par
  categorie) et "DigiPaint(TM)" (/paint/ = les toiles perso de l'utilisateur). Fini le doublon.
- Jeu : la palette ne mange plus l'ecran. Un bouton flottant "Peinture" (avec la couleur
  courante) ouvre un TIROIR de couleurs scrollable (bottom sheet, max 48vh) ; choisir une couleur
  referme le tiroir. Bien meilleur sur smartphone.

## Home slider rempli<->vide numerote + seed_library refait galerie & home (MAJ)
- Home : le slider compare desormais la toile COLORIEE (rempli) et la toile NUMEROTEE VIDE
  (SVG template), en auto-animation (oscillation), pause des qu'on le prend en main. Rotation
  moto <-> autres.
- seed_library : regenere en une commande le contenu de la GALERIE (modeles PaintIt) ET les
  assets d'animation du HOME (pour chaque modele : *_pbn.png colorie + *_template.png numerote
  dans static/studio/showcase/). Pense a `collectstatic` en prod apres le seed.

## Preview fix + palette en roue infinie (MAJ)
- Preview : correction de l'erreur JS "Cannot set properties of null" (upd protege quand le total
  #ptotal est absent).
- Jeu : la palette devient une ROUE INFINIE de couleurs qui defile en bas (~5 visibles, boucle
  sans couture via 2 copies + animation CSS). Chaque item = un pot BRILLANT + son numero. Clic =
  selection. Pause au survol (PC) et au toucher (mobile) pour viser. Les 3 pots libres sont
  integres dans la roue. Controles (zoom, minimap, progression, aide) remontes au-dessus du bandeau.

## seed_library sans cairosvg (Windows OK) (MAJ)
- seed_library ne depend plus de cairosvg (penible a installer sous Windows). Les assets du home
  (colorie _pbn.png + numerote _template.png) sont copies/redimensionnes depuis les PNG deja
  produits par le pipeline (via Pillow, portable). La generation des assets home est aussi
  encapsulee : une erreur showcase n'interrompt plus la generation de la galerie.

## Roue controlee (drag/fleches) + Ctrl+Z + email prod (MAJ)
- Palette : la roue de pots ne defile plus toute seule. L'utilisateur la fait tourner au DRAG
  (souris) ou avec les fleches <- ->. Elle est RANGEE par defaut (poignee en bas) et se deplie au
  survol (PC) ou au tap (mobile) ; elle se range quand la souris la quitte / tap ailleurs.
- Jeu : Ctrl+Z (ou Cmd+Z) annule les dernieres actions de peinture (pile d'historique ~50).
- E-mail prod : envois robustes (jamais de crash de requete) + logs des echecs (LOGGING ->
  journalctl). SMTP Hostinger + contact@paintit.click deja configures.

## Tableau fini en "Bientot" + liste d'attente (MAJ)
- Le concept du tableau imprime est explique, mais le PROCESSUS D'ACHAT est remplace par
  "Bientot disponible" (le temps de choisir un fournisseur d'impression). Un champ e-mail
  "Prevenez-moi" alimente une liste d'attente (visible dans l'admin ContactMessage).
- La commande (/print/<uid>/order/) est desactivee (redirige). Le bouton "Tableau fini" des
  cartes galerie porte un badge "bientot".
