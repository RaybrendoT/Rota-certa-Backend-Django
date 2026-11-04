from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .models import AccessAccount, Route, RoutePassenger
from .realtime import person_group, route_group

# Fechamento quando o token falta, é inválido ou expirou: o app renova e reconecta.
CLOSE_UNAUTHORIZED = 4001


@database_sync_to_async
def _route_ids_for(user):
    """Rotas cujas mensagens o usuário recebe: as que dirige, as que usa e, para gestor, as da empresa."""
    person = user.person
    if user.role == AccessAccount.Role.DRIVER:
        return set(
            Route.objects.filter(is_active=True, vehicle__driver=person).values_list("id", flat=True)
        )
    ids = set(
        RoutePassenger.objects.filter(
            person=person, is_active=True, route__is_active=True
        ).values_list("route_id", flat=True)
    )
    if user.role == AccessAccount.Role.MANAGER:
        ids |= set(
            Route.objects.filter(is_active=True, company_id=person.company_id).values_list("id", flat=True)
        )
    return ids


class StreamConsumer(AsyncJsonWebsocketConsumer):
    """
    /ws/stream/?token=<access_token> — canal único servidor → app.

    Os grupos são calculados na conexão; se a pessoa ganhar ou perder rotas,
    passa a valer na próxima reconexão.
    """

    async def connect(self):
        # Aceita antes de recusar para o cliente receber o código 4001 (e não um 403 genérico).
        await self.accept()
        user = self.scope.get("user")
        if user is None:
            await self.close(code=CLOSE_UNAUTHORIZED)
            return

        self.groups_joined = [person_group(user.person.id)]
        self.groups_joined += [route_group(rid) for rid in await _route_ids_for(user)]
        for group in self.groups_joined:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.send_json({"type": "connected", "person_id": user.person.id})

    async def disconnect(self, code):
        for group in getattr(self, "groups_joined", []):
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get("type") == "ping":
            await self.send_json({"type": "pong"})

    async def stream_message(self, event):
        await self.send_json(event["payload"])
