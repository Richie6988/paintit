"""Soumet toutes les URL du sitemap a IndexNow (Bing, Yandex, Seznam, Naver ; Brave s'appuie en partie sur ces signaux).
A lancer apres chaque deploiement qui ajoute ou modifie des pages :  python manage.py seo_ping
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Soumet les URL publiques a IndexNow."

    def handle(self, *args, **opts):
        from studio import seo
        urls = seo.sitemap_urls()
        try:
            code = seo.indexnow_submit(urls)
        except Exception as exc:
            self.stdout.write(self.style.ERROR("IndexNow : %s" % exc))
            return
        self.stdout.write(self.style.SUCCESS("IndexNow : %d URL soumises (HTTP %s)." % (len(urls), code)))
