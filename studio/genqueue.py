"""File d'attente bornee pour la generation d'images (anti-saturation / DDOS).

La generation (OpenCV + HED) est lourde. Sans borne, chaque requete /create/
lancait un thread : un afflux de requetes pouvait saturer le CPU/la memoire et
faire tomber tout le site (502).

Ici, chaque worker gunicorn possede un pool borne. La concurrence totale de
generation est donc (nb workers) x GEN_MAX_WORKERS, avec un backlog borne par
GEN_MAX_PENDING. Au-dela, submit() leve QueueFull et l'appelant renvoie une
reponse "serveur occupe" (429) plutot que de lancer un thread de plus.

Reglable par variables d'environnement :
  GEN_MAX_WORKERS  (defaut 2)  generations simultanees par worker
  GEN_MAX_PENDING  (defaut 8)  taille max de la file d'attente par worker
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

MAX_WORKERS = max(1, int(os.environ.get("GEN_MAX_WORKERS", "2")))
MAX_PENDING = max(0, int(os.environ.get("GEN_MAX_PENDING", "8")))
_CAP = MAX_WORKERS + MAX_PENDING

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="gen")
_lock = threading.Lock()
_inflight = 0  # en cours d'execution + en attente dans la file


class QueueFull(Exception):
    """Trop de generations en cours ou en attente : rejeter la requete."""


def submit(fn, *args, **kwargs):
    """Planifie fn(*args, **kwargs) dans le pool borne.

    Retourne un Future. Leve QueueFull si la capacite est atteinte.
    """
    global _inflight
    with _lock:
        if _inflight >= _CAP:
            raise QueueFull()
        _inflight += 1

    def _runner():
        global _inflight
        try:
            return fn(*args, **kwargs)
        finally:
            with _lock:
                _inflight -= 1

    try:
        return _executor.submit(_runner)
    except RuntimeError:
        # Pool en cours d'arret : on relache le compteur et on relaie.
        with _lock:
            _inflight -= 1
        raise QueueFull()


def stats():
    with _lock:
        return {"inflight": _inflight, "capacity": _CAP,
                "workers": MAX_WORKERS, "pending_max": MAX_PENDING}
