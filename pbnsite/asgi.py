"""Point d'entree ASGI : utilise uniquement par le service WebSocket (paintit-ws, uvicorn).
Le site lui-meme reste servi par gunicorn en WSGI (pbnsite.wsgi)."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pbnsite.settings")
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from studio.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(URLRouter(websocket_urlpatterns)),
})
