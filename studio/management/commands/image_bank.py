from django.core.management.base import BaseCommand

from studio import imagebank


class Command(BaseCommand):
    help = "Remplit la banque d'images libres (Openverse, Wikimedia Commons, + Pixabay / Pexels si cles API)."

    def add_arguments(self, parser):
        parser.add_argument("--per-query", type=int, default=30, help="images demandees par recherche et par source")
        parser.add_argument("--min-vibrancy", type=float, default=imagebank.MIN_VIBRANCY)
        parser.add_argument("--category", action="append", help="theme (repetable) ; defaut : tous")
        parser.add_argument("--provider", action="append", choices=imagebank.PROVIDERS, help="source (repetable)")

    def handle(self, *args, **o):
        provs = [p for p in (o["provider"] or imagebank.available())]
        self.stdout.write("Sources : %s · %d recherches" % (", ".join(provs),
                                                            imagebank.total_queries(o["category"], provs)))
        added, skipped = imagebank.harvest(categories=o["category"], providers=provs, per_query=o["per_query"],
                                           min_vibrancy=o["min_vibrancy"], log=self.stderr.write)
        self.stdout.write(self.style.SUCCESS("%d images ajoutées, %d ignorées (doublons, trop petites ou ternes)."
                                             % (added, skipped)))
