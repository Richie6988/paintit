"""Sauvegarde de la base + des medias utiles, avec rotation.

    python manage.py backup_data                      # -> $BACKUP_DIR (defaut ../backups), garde 14 jours
    python manage.py backup_data --keep 30 --dir /mnt/backups

Cron quotidien conseille (utilisateur paintit) :
    30 3 * * * cd /home/paintit/Django && venv/bin/python manage.py backup_data >> /home/paintit/backup.log 2>&1
Puis copier le dossier hors du VPS (rclone, rsync vers un autre serveur, stockage objet...).
"""
import os
import sqlite3
import subprocess
import tarfile
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

SKIP_MEDIA = ("uploads",)   # photos brutes temporaires : inutile de les sauvegarder


class Command(BaseCommand):
    help = "Sauvegarde la base de donnees et media/ (hors uploads temporaires), avec rotation."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=os.environ.get(
            "BACKUP_DIR", os.path.join(os.path.dirname(str(settings.BASE_DIR)), "backups")))
        parser.add_argument("--keep", type=int, default=int(os.environ.get("BACKUP_KEEP", "14")))
        parser.add_argument("--no-media", action="store_true")

    def handle(self, *args, **opts):
        out = opts["dir"]
        os.makedirs(out, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        db = settings.DATABASES["default"]

        if connection.vendor == "sqlite":
            dst = os.path.join(out, "db-%s.sqlite3" % stamp)
            src = sqlite3.connect(str(db["NAME"]))
            with sqlite3.connect(dst) as bak:
                src.backup(bak)          # copie coherente meme si le site ecrit en meme temps
            src.close()
        elif connection.vendor == "postgresql":
            dst = os.path.join(out, "db-%s.dump" % stamp)
            env = dict(os.environ, PGPASSWORD=str(db.get("PASSWORD") or ""))
            cmd = ["pg_dump", "-Fc", "-f", dst, "-h", str(db.get("HOST") or "localhost"),
                   "-p", str(db.get("PORT") or 5432), "-U", str(db.get("USER") or ""), str(db["NAME"])]
            if subprocess.call(cmd, env=env):
                raise CommandError("pg_dump a echoue")
        else:
            raise CommandError("Base %s non geree" % connection.vendor)
        self.stdout.write("Base : %s (%d Ko)" % (dst, os.path.getsize(dst) // 1024))

        if not opts["no_media"] and os.path.isdir(settings.MEDIA_ROOT):
            tgz = os.path.join(out, "media-%s.tar.gz" % stamp)
            with tarfile.open(tgz, "w:gz") as tar:
                for name in os.listdir(settings.MEDIA_ROOT):
                    if name not in SKIP_MEDIA:
                        tar.add(os.path.join(settings.MEDIA_ROOT, name), arcname=os.path.join("media", name))
            self.stdout.write("Medias : %s (%d Mo)" % (tgz, os.path.getsize(tgz) // (1024 * 1024)))

        # Rotation : on garde les `keep` sauvegardes les plus recentes de chaque type
        for prefix in ("db-", "media-"):
            files = sorted(f for f in os.listdir(out) if f.startswith(prefix))
            for old in files[:-opts["keep"]] if opts["keep"] > 0 else []:
                os.remove(os.path.join(out, old))
        self.stdout.write(self.style.SUCCESS("Sauvegarde terminee."))
