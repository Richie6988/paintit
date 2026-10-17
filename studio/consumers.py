"""Peindre a plusieurs en temps reel (WebSocket) : zones peintes diffusees instantanement et curseurs des
participants (points de couleur). L'etat est enregistre dans CollabRoom, comme pour le mode HTTP de secours."""
import random

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

COLORS = ["#e8455f", "#2f6bf2", "#1f9d55", "#f08a3c", "#7a3ff2", "#0ea5b7", "#d63384", "#8a6d1f"]


@database_sync_to_async
def _room(token):
    from .models import CollabRoom
    r = CollabRoom.objects.filter(token=token).first()
    return (r.painted, r.version) if r else None


@database_sync_to_async
def _presence(token, me, score=None, name=None, leave=False):
    """Meme registre de joueurs que le mode HTTP : le classement reunit les deux modes."""
    from django.core.cache import cache
    from .views import _collab_players
    if leave:
        peers = cache.get("collab-peers:" + token) or {}
        peers.pop(me["id"], None)
        cache.set("collab-peers:" + token, peers, 300)
        return
    _collab_players(token, me["id"], score, name, me["c"])


@database_sync_to_async
def _save(token, zones):
    """Ajoute les zones encore libres (la premiere couleur gagne) -> (zones ajoutees, version)."""
    from django.db import transaction
    from .models import CollabRoom
    with transaction.atomic():
        r = CollabRoom.objects.select_for_update().filter(token=token).first()
        if not r:
            return {}, 0
        new = {k: v for k, v in zones.items() if k not in r.painted}
        if new:
            r.painted.update(new)
            r.version += 1
            r.save(update_fields=["painted", "version", "updated_at"])
        return new, r.version


class CollabConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.token = self.scope["url_route"]["kwargs"]["token"][:24]
        state = await _room(self.token)
        if state is None:
            await self.close(code=4404)
            return
        self.group = "collab_" + "".join(c for c in self.token if c.isalnum() or c in "-_")
        self.me = {"id": "%06x" % random.getrandbits(24), "c": random.choice(COLORS)}
        self.last_cur = 0.0
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_json({"t": "state", "painted": state[0], "v": state[1], "me": self.me})
        await self._broadcast({"t": "hello", **self.me})

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await _presence(self.token, self.me, leave=True)
            await self._broadcast({"t": "bye", "id": self.me["id"]})
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, msg, **kw):
        t = msg.get("t")
        if t == "paint":
            from .views import _clean_zones
            zones = _clean_zones(msg.get("z") if isinstance(msg.get("z"), dict) else {}, limit=200)
            if zones:
                new, v = await _save(self.token, zones)
                if new:
                    await self._broadcast({"t": "paint", "z": new, "v": v, "id": self.me["id"]})
        elif t == "cur":
            import time
            now = time.monotonic()
            if now - self.last_cur < 0.05:                     # 20 positions/s max par participant
                return
            self.last_cur = now
            try:
                x, y = float(msg.get("x")), float(msg.get("y"))
            except (TypeError, ValueError):
                return
            name = str(msg.get("n") or "")[:16]
            await self._broadcast({"t": "cur", "x": round(x, 1), "y": round(y, 1), "n": name, **self.me})
        elif t in ("here", "score"):                            # presence (+ reponse a un « hello ») et classement
            import time
            now = time.monotonic()
            if t == "score" and now - getattr(self, "last_score", 0) < 0.25:
                return
            self.last_score = now
            try:
                pts = max(0, min(10 ** 9, int(msg.get("s") or 0)))
            except (TypeError, ValueError):
                pts = 0
            name = str(msg.get("n") or "")[:16]
            await _presence(self.token, self.me, pts, name)
            await self._broadcast({"t": t, "s": pts, "n": name, **self.me})

    async def _broadcast(self, data):
        await self.channel_layer.group_send(self.group, {"type": "relay", "data": data})

    async def relay(self, event):
        await self.send_json(event["data"])
