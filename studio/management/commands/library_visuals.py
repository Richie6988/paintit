"""Genere les visuels manquants de la bibliotheque de publications (a lancer une fois apres deploiement)."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Genere les visuels manquants de la bibliotheque de publications."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--refresh-gifs", action="store_true",
                            help="Re-rend tous les GIF existants avec la mise en page actuelle (memes textes et liens).")

    def handle(self, *args, **opts):
        from studio import library_visuals
        if opts["refresh_gifs"]:
            from studio import marketing
            self.stdout.write(self.style.SUCCESS("%d GIF regénéré(s)." % marketing.refresh_gifs()))
            return
        if not library_visuals.base_canvases():
            self.stdout.write(self.style.ERROR("Aucune toile du catalogue utilisable comme photo de base."))
            return
        n = library_visuals.build_missing(limit=opts["limit"])
        self.stdout.write(self.style.SUCCESS("%d visuel(s) généré(s)." % n))
