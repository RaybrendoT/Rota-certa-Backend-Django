"""
ASGI: HTTP (Django) + WebSocket (Channels) no mesmo processo.

Uso: daphne -b 0.0.0.0 -p 8000 config.asgi:application
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Inicializa o Django antes de importar qualquer coisa que toque nos models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402

from core.routing import websocket_urlpatterns  # noqa: E402
from core.ws_auth import JWTQueryStringAuthMiddleware  # noqa: E402

# Sem nginx na frente, o próprio processo serve /static/ (admin e Swagger).
http_app = ASGIStaticFilesHandler(django_asgi_app) if settings.SERVE_STATIC_FILES else django_asgi_app

# Sem validação de Origin: o app Android não envia esse cabeçalho, e o acesso
# já exige o JWT na query string (não há cookie a ser explorado entre sites).
application = ProtocolTypeRouter(
    {
        "http": http_app,
        "websocket": JWTQueryStringAuthMiddleware(URLRouter(websocket_urlpatterns)),
    }
)
