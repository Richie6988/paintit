"""Suivi de la generation asynchrone via le cache Django.

En dev : LocMemCache. En prod multi-worker : definir REDIS_URL pour un cache partage,
sinon la page de progression et la generation peuvent tomber sur des workers differents.
L'etat (progression + resultat) est donc stocke dans le cache, pas en memoire locale.
"""
from django.core.cache import cache

TTL = 900  # 15 min


def _key(uid):
    return f"pbngen:{uid}"


def start(uid):
    cache.set(_key(uid), {"pct": 0, "label": "read",
                          "done": False, "error": None, "order": None}, TTL)


def update(uid, **kw):
    j = cache.get(_key(uid)) or {"pct": 0, "label": "", "done": False,
                                 "error": None, "order": None}
    j.update(kw)
    cache.set(_key(uid), j, TTL)
    return j


def get(uid):
    return cache.get(_key(uid))


def pop(uid):
    j = cache.get(_key(uid))
    cache.delete(_key(uid))
    return j
