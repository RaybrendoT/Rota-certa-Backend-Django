"""
Regras de notificação e mensagens em tempo real do app mobile.

Toda notificação é gravada (aba Avisos tem histórico) e enviada ao grupo
person_<id> do WebSocket. Mensagens de estado da rota (posição, eventos,
embarques) vão para o grupo route_<id>.
"""
from django.utils import timezone

from .driver_serializers import route_status_for_event_type
from .geo import distance_meters
from .models import Notification, RoutePassenger
from .push import push_on_commit
from .realtime import broadcast_on_commit, person_group, route_group
from .realtime_serializers import NotificationSerializer, location_payload

# Evento "stop" a até esta distância da parada do passageiro = ônibus no ponto dele.
STOP_RADIUS_METERS = 200
# Ônibus a até esta distância da parada = aviso de "chegando" (uma vez por dia).
APPROACHING_RADIUS_METERS = 1000

Kind = Notification.Kind


def notify(recipients, *, kind, title, message="", route=None, data=None):
    """Grava uma notificação por destinatário e a entrega pelo WebSocket e por push após o commit."""
    created = Notification.objects.bulk_create(
        [
            Notification(
                recipient=person,
                route=route,
                kind=kind,
                title=title,
                message=message,
                data=data or {},
            )
            for person in recipients
        ]
    )
    for notification in created:
        broadcast_on_commit(
            person_group(notification.recipient_id),
            {"type": "notification", "notification": NotificationSerializer(notification).data},
        )
    push_on_commit(created)
    return created


def _active_passengers(route):
    return RoutePassenger.objects.filter(route=route, is_active=True).select_related(
        "person", "route_stop__meeting_point__address"
    )


def _stop_address(passenger):
    stop = passenger.route_stop
    if stop is None or not stop.is_active:
        return None
    return stop.meeting_point.address


_EVENT_MESSAGES = {
    "departure": (Kind.ROUTE_DEPARTURE, "Seu ônibus saiu", "A rota {route} foi iniciada."),
    "arrival": (Kind.ROUTE_ARRIVAL, "Rota finalizada", "A rota {route} chegou ao destino."),
    "delay": (Kind.ROUTE_DELAY, "Atraso na rota {route}", "{description}"),
    "incident": (Kind.ROUTE_INCIDENT, "Incidente na rota {route}", "{description}"),
}


def on_route_event_created(event):
    route = event.route
    broadcast_on_commit(
        route_group(route.id),
        {
            "type": "route_event",
            "id": event.id,
            "route_id": route.id,
            "route_name": route.name,
            "event_type": event.event_type,
            "description": event.description,
            "route_status": route_status_for_event_type(event.event_type),
            "timestamp": event.timestamp.isoformat(),
        },
    )

    passengers = list(_active_passengers(route))
    if event.event_type in _EVENT_MESSAGES:
        kind, title, message = _EVENT_MESSAGES[event.event_type]
        fmt = {"route": route.name, "description": event.description}
        notify(
            [p.person for p in passengers],
            kind=kind,
            title=title.format(**fmt),
            message=message.format(**fmt),
            route=route,
        )
    elif event.event_type == "stop":
        at_stop = [
            p
            for p in passengers
            if (addr := _stop_address(p))
            and distance_meters(event.latitude, event.longitude, addr.latitude, addr.longitude)
            <= STOP_RADIUS_METERS
        ]
        for p in at_stop:
            notify(
                [p.person],
                kind=Kind.BUS_AT_STOP,
                title="O ônibus chegou ao seu ponto",
                message=f"A rota {route.name} está em {p.route_stop.meeting_point.name}.",
                route=route,
            )


def on_boarding_status_changed(passenger, *, by_passenger):
    """Avisa a rota (lista do motorista ao vivo) e, se foi o colaborador quem mudou, o motorista."""
    route = passenger.route
    broadcast_on_commit(
        route_group(route.id),
        {
            "type": "boarding_status",
            "route_id": route.id,
            "route_name": route.name,
            "route_passenger_id": passenger.id,
            "person_id": passenger.person_id,
            "person_name": passenger.person.name,
            "status": passenger.boarding_status,
            "timestamp": passenger.updated_at.isoformat(),
        },
    )

    driver = route.vehicle.driver if route.vehicle_id and route.vehicle.driver_id else None
    if not by_passenger or driver is None:
        return
    if passenger.boarding_status == RoutePassenger.BoardingStatus.CONFIRMED:
        kind, title = Kind.BOARDING_CONFIRMED, f"{passenger.person.name} confirmou embarque"
    elif passenger.boarding_status == RoutePassenger.BoardingStatus.ABSENT:
        kind, title = Kind.BOARDING_ABSENT, f"{passenger.person.name} não vai hoje"
    else:
        return
    point = passenger.route_stop.meeting_point.name if passenger.route_stop_id else route.name
    notify(
        [driver],
        kind=kind,
        title=title,
        message=f"Ponto: {point}",
        route=route,
        data={"route_passenger_id": passenger.id},
    )


def on_vehicle_location(location):
    route = location.route
    broadcast_on_commit(route_group(route.id), {"type": "vehicle_location", **location_payload(location)})

    today = timezone.localdate()
    already_notified = set(
        Notification.objects.filter(
            route=route, kind=Kind.BUS_APPROACHING, created_at__date=today
        ).values_list("recipient_id", flat=True)
    )
    for p in _active_passengers(route):
        addr = _stop_address(p)
        if addr is None or p.person_id in already_notified:
            continue
        distance = distance_meters(location.latitude, location.longitude, addr.latitude, addr.longitude)
        if STOP_RADIUS_METERS < distance <= APPROACHING_RADIUS_METERS:
            notify(
                [p.person],
                kind=Kind.BUS_APPROACHING,
                title="Ônibus chegando ao seu ponto",
                message=f"A rota {route.name} está a cerca de {round(distance, -1):.0f} m de {p.route_stop.meeting_point.name}.",
                route=route,
            )
