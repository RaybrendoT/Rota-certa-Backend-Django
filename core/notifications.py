"""
Regras de notificação e mensagens em tempo real do app mobile.

Toda notificação é gravada (aba Avisos tem histórico) e enviada ao grupo
person_<id> do WebSocket. Mensagens de estado da rota (posição, eventos,
embarques) vão para o grupo route_<id>.
"""
import re
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import eta
from .dates import local_today
from .driver_serializers import route_status_for_event_type
from .geo import distance_meters
from .models import EtaSample, Notification, RoutePassenger, StopArrival, VehicleLocation
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


def route_ref(name):
    """'rota X' nas mensagens, sem repetir quando o nome já começa com 'Rota' ("A Rota Manaus 01 ...")."""
    name = (name or "").strip()
    return name if re.match(r"rota\b", name, re.IGNORECASE) else f"rota {name}"


_EVENT_MESSAGES = {
    "departure": (Kind.ROUTE_DEPARTURE, "Seu ônibus saiu", "A {route} foi iniciada."),
    "arrival": (Kind.ROUTE_ARRIVAL, "Rota finalizada", "A {route} chegou ao destino."),
    "delay": (Kind.ROUTE_DELAY, "Atraso na {route}", "{description}"),
    "incident": (Kind.ROUTE_INCIDENT, "Incidente na {route}", "{description}"),
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

    # Ocorrências do sistema (e as do gestor sem "avisar passageiros") não vão para os passageiros.
    if not getattr(event, "notify_passengers", True):
        return
    passengers = list(_active_passengers(route))
    if event.event_type in _EVENT_MESSAGES:
        kind, title, message = _EVENT_MESSAGES[event.event_type]
        fmt = {"route": route_ref(route.name), "description": event.description}
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
            if event.latitude is not None
            and (addr := _stop_address(p))
            and distance_meters(event.latitude, event.longitude, addr.latitude, addr.longitude)
            <= STOP_RADIUS_METERS
        ]
        for p in at_stop:
            notify(
                [p.person],
                kind=Kind.BUS_AT_STOP,
                title="O ônibus chegou ao seu ponto",
                message=f"A {route_ref(route.name)} está em {p.route_stop.meeting_point.name}.",
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


def record_vehicle_location(route, data, recorded_at=None):
    """
    Grava a última posição do ônibus na rota (API do motorista e simulador), calcula o
    progresso no trajeto e dispara os avisos.
    """
    previous = VehicleLocation.objects.filter(route=route).values_list("progress_meters", flat=True).first()
    location, _ = VehicleLocation.objects.update_or_create(
        route=route,
        defaults={
            "latitude": round(Decimal(str(data["latitude"])), 6),
            "longitude": round(Decimal(str(data["longitude"])), 6),
            "heading": data.get("heading"),
            "speed": data.get("speed"),
            "accuracy": data.get("accuracy"),
            "recorded_at": recorded_at or timezone.now(),
        },
    )
    eta.update_progress(location, previous_along=previous)
    on_vehicle_location(location)
    return location


def on_vehicle_location(location):
    route = location.route
    broadcast_on_commit(route_group(route.id), {"type": "vehicle_location", **location_payload(location)})

    today = local_today()
    already_notified = set(
        Notification.objects.filter(
            route=route, kind=Kind.BUS_APPROACHING, created_at__date=timezone.localdate()
        ).values_list("recipient_id", flat=True)
    )
    approaching_minutes = getattr(settings, "BUS_APPROACHING_MINUTES", 10)
    path = eta.route_path(route)
    for p in _active_passengers(route):
        addr = _stop_address(p)
        if addr is None:
            continue
        estimate = eta.estimate(p, location=location, path=path)
        _record_eta(p, estimate, location, today)
        if p.person_id in already_notified:
            continue
        if estimate["minutes"] is not None:
            # Previsão pelo trajeto: avisa quando faltam poucos minutos (e o ônibus ainda não está no ponto).
            should_notify = (
                estimate["phase"] in (eta.Phase.APPROACHING, eta.Phase.ARRIVING)
                and estimate["distance_meters"] > STOP_RADIUS_METERS
                and estimate["minutes"] <= approaching_minutes
            )
            message = (
                f"A {route_ref(route.name)} chega a {p.route_stop.meeting_point.name} "
                f"em cerca de {estimate['minutes']} min."
            )
        else:
            # Sem trajeto calculável: regra de distância em linha reta.
            distance = distance_meters(location.latitude, location.longitude, addr.latitude, addr.longitude)
            should_notify = STOP_RADIUS_METERS < distance <= APPROACHING_RADIUS_METERS
            message = (
                f"A {route_ref(route.name)} está a cerca de {round(distance, -1):.0f} m de "
                f"{p.route_stop.meeting_point.name}."
            )
        if should_notify:
            notify(
                [p.person],
                kind=Kind.BUS_APPROACHING,
                title="Ônibus chegando ao seu ponto",
                message=message,
                route=route,
                data={"eta_minutes": estimate["minutes"]} if estimate["minutes"] is not None else None,
            )


def _record_eta(passenger, estimate, location, service_date):
    """Guarda a previsão (a cada ETA_SAMPLE_SECONDS) e a chegada real, para o relatório do teste em campo."""
    if not estimate["is_live"] or (estimate["seconds"] is None and estimate["phase"] != eta.Phase.PASSED):
        return
    arrived = estimate["phase"] == eta.Phase.PASSED or (
        estimate["distance_meters"] is not None and estimate["distance_meters"] <= eta.REACHED_TOLERANCE_M * 2
    )
    if arrived:
        if not StopArrival.objects.filter(passenger=passenger, service_date=service_date).exists():
            try:
                with transaction.atomic():
                    StopArrival.objects.create(passenger=passenger, service_date=service_date,
                                               arrived_at=location.recorded_at)
            except IntegrityError:
                pass
        return
    interval = timedelta(seconds=getattr(settings, "ETA_SAMPLE_SECONDS", 30))
    last = (
        EtaSample.objects.filter(passenger=passenger, service_date=service_date)
        .order_by("-recorded_at").values_list("recorded_at", flat=True).first()
    )
    if last is not None and location.recorded_at - last < interval:
        return
    EtaSample.objects.create(
        passenger=passenger, service_date=service_date, recorded_at=location.recorded_at,
        eta_seconds=estimate["seconds"], distance_meters=estimate["distance_meters"], speed=location.speed,
    )
