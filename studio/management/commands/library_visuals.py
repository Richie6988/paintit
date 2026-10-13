"""Genere les visuels manquants de la bibliotheque de publications (a lancer une fois apres deploiement)."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Genere les visuels manquants de la bibliotheque de publications."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=200)

    def handle(self, *args, **opts):
        from studio import library_visuals
        if not library_visuals.base_canvases():
            self.stdout.write(self.style.ERROR("Aucune toile du catalogue utilisable comme photo de base."))
            return
        n = library_visuals.build_missing(limit=opts["limit"])
        self.stdout.write(self.style.SUCCESS("%d visuel(s) généré(s)." % n))
