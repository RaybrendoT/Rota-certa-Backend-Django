"""Autenticação do WebSocket pelo mesmo JWT da API, enviado em ?token=<access_token>."""
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.middleware import BaseMiddleware
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken, TokenError

from .authentication import AccessAccountJWTAuthentication


@database_sync_to_async
def _authenticate(raw_token):
    auth = AccessAccountJWTAuthentication()
    try:
        user = auth.get_user(auth.get_validated_token(raw_token))
    except (InvalidToken, AuthenticationFailed, TokenError):
        return None
    return user if user.is_active else None


class JWTQueryStringAuthMiddleware(BaseMiddleware):
    """Coloca em scope["user"] o AccessAccountPrincipal, ou None se o token for inválido."""

    async def __call__(self, scope, receive, send):
        query = parse_qs(scope.get("query_string", b"").decode())
        token = (query.get("token") or [None])[0]
        scope = dict(scope, user=await _authenticate(token) if token else None)
        return await super().__call__(scope, receive, send)
