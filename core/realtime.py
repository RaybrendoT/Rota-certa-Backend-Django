"""
Envio de mensagens para o WebSocket (/ws/stream/) via channel layer.

Grupos:
- person_<id>: mensagens de uma pessoa (notificações).
- route_<id>:  mensagens de uma rota (posição do ônibus, eventos, embarques).
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction


def person_group(person_id) -> str:
    return f"person_{person_id}"


def route_group(route_id) -> str:
    return f"route_{route_id}"


def send_to_group(group: str, payload: dict) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(group, {"type": "stream.message", "payload": payload})


def broadcast_on_commit(group: str, payload: dict) -> None:
    """Só envia depois do commit, para o cliente nunca ver dado que sofreu rollback."""
    transaction.on_commit(lambda: send_to_group(group, payload))
