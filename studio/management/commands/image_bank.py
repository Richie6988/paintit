from django.core.management.base import BaseCommand

from studio import imagebank


class Command(BaseCommand):
    help = ("Remplit la banque d'images libres (Openverse, Wikimedia Commons, Art Institute of Chicago, Met Museum, "
            "+ Pixabay / Pexels si cles API).")

    def add_arguments(self, parser):
        parser.add_argument("--per-query", type=int, default=30, help="images demandees par recherche et par source")
        parser.add_argument("--min-vibrancy", type=float, default=imagebank.MIN_VIBRANCY)
        parser.add_argument("--category", action="append", help="theme (repetable) ; defaut : tous")
        parser.add_argument("--provider", action="append", choices=imagebank.PROVIDERS, help="source (repetable)")
        parser.add_argument("--openverse-register", metavar="EMAIL",
                            help="cree un compte API Openverse gratuit (quotas plus larges) et affiche les identifiants")

    def _out(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()                      # log lisible en direct, meme redirige vers un fichier (nohup)

    def handle(self, *args, **o):
        if o["openverse_register"]:
            d = imagebank.openverse_register(o["openverse_register"])
            self._out("Ajoutez ces deux lignes dans .env puis cliquez le lien reçu par e-mail :\n"
                      "OPENVERSE_CLIENT_ID=%s\nOPENVERSE_CLIENT_SECRET=%s" % (d.get("client_id"), d.get("client_secret")))
            return
        provs = [p for p in (o["provider"] or imagebank.available())]
        self._out("Sources : %s · %d recherches" % (", ".join(provs), imagebank.total_queries(o["category"], provs)))
        added, skipped = imagebank.harvest(categories=o["category"], providers=provs, per_query=o["per_query"],
                                           min_vibrancy=o["min_vibrancy"], log=self._out, info=self._out)
        self._out(self.style.SUCCESS("%d images ajoutées, %d ignorées (doublons, trop petites ou ternes)."
                                     % (added, skipped)))
