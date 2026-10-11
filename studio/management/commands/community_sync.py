"""Suivi des publications communautes (cron, ex. toutes les heures) :
    0 * * * * cd /home/paintit/Django && .venv/bin/python manage.py community_sync
- actualise score / commentaires des publications Reddit des 30 derniers jours ;
- e-mail d'alerte quand de nouvelles reponses arrivent (pour repondre vite, en tant que PaintIt) ;
- le lundi : actualise membres + regles des subreddits de l'annuaire.
"""
import datetime
import time

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from studio import community as cm
from studio.models import CompanyInfo, Community, Outreach


class Command(BaseCommand):
    help = "Actualise les statistiques des publications communautes et alerte en cas de nouvelles reponses."

    def handle(self, *args, **opts):
        since = timezone.now() - datetime.timedelta(days=30)
        alerts = []
        for o in Outreach.objects.filter(posted_at__gte=since, posted_url__contains="reddit.com").select_related("community"):
            before = o.comments or 0
            try:
                cm.refresh_post(o)
            except Exception as exc:
                self.stdout.write(self.style.ERROR("%s : %s" % (o.code, exc)))
                continue
            if (o.comments or 0) > before and (o.comments or 0) > o.comments_seen:
                alerts.append(o)
            time.sleep(1)
        if alerts:
            lines = ["Nouvelles reponses a vos publications :", ""]
            lines += ["- %s : %s (+%d) %s" % (o.community.name if o.community else "?", o.title[:80], o.new_replies,
                                             o.posted_url) for o in alerts]
            lines += ["", "Tableau de bord : %s/admin-hub/communities/" % settings.SITE_URL]
            send_mail("PaintIt · %d publication(s) avec de nouvelles reponses" % len(alerts), "\n".join(lines),
                      settings.DEFAULT_FROM_EMAIL, [CompanyInfo.get().email], fail_silently=True)
        if timezone.localdate().weekday() == 0 and timezone.localtime().hour < 1:
            for c in Community.objects.filter(platform="reddit", active=True):
                try:
                    cm.refresh_subreddit(c)
                except Exception:
                    pass
                time.sleep(1)
        self.stdout.write("%d alerte(s)" % len(alerts))
