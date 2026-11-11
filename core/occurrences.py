"""
Ocorrências de rota: atrasos e incidentes (RouteEvent com event_type delay/incident).

Fluxo
-----
Origem (`source`):
- motorista: enviada pelo app durante a rota (com a posição do ônibus);
- gestor: registrada no painel (ex.: o motorista ligou avisando);
- sistema: detectada aqui, a partir do horário do turno e dos registros de partida/chegada:
    * not_started  — passou da saída prevista (+ tolerância) e não há partida registrada;
    * overdue      — passou do horário de chegada (+ tolerância) e não há chegada registrada;
    * late_arrival — a chegada foi registrada depois do horário (+ tolerância).

Status: aberta → em andamento (o gestor assumiu) → resolvida (com quem resolveu e como). As do
sistema se resolvem sozinhas quando o motivo some: partida registrada resolve a not_started e
chegada registrada resolve a overdue. Uma ocorrência resolvida pode ser reaberta.

A detecção roda quando o painel abre o dashboard ou a lista de ocorrências (no máximo uma vez por
minuto por empresa) e pelo comando `python manage.py detectar_ocorrencias`, para agendar no cron.
Ocorrências do sistema não mandam push aos passageiros; as do motorista sim (como antes).
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from core import route_optimizer
from core.models import RouteEvent, RouteStop
from core.self_address import company_location

TOLERANCE_MINUTES = 10
DEPARTURE_MARGIN_MINUTES = 5
DETECT_EVERY_SECONDS = 60

Source = RouteEvent.Source
Status = RouteEvent.Status


class OccurrenceError(Exception):
    """Ação que não vale para o evento (ex.: resolver uma partida)."""


# ---------------------------------------------------------------------- detecção automática

def detect(company, now=None, *, force=False) -> int:
    """Cria/resolve as ocorrências do sistema do dia de hoje. Devolve quantas criou."""
    tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
    now = (now or timezone.now()).astimezone(tz)
    key = f"occurrences:detect:{company.pk}"
    if not force and cache.get(key):
        return 0
    cache.set(key, True, DETECT_EVERY_SECONDS)

    from core.dashboard import routes_of_day  # evita import circular (o dashboard usa este módulo)

    day = now.date()
    routes = [route for route in routes_of_day(company, day) if route.schedule_id]
    if not routes:
        return 0
    by_id = {route.id: route for route in routes}

    departures, arrivals, system = {}, {}, {}
    for event in RouteEvent.objects.filter(route_id__in=by_id, is_active=True, service_date=day).order_by("timestamp"):
        if event.event_type == "departure":
            departures.setdefault(event.route_id, event.timestamp.astimezone(tz))
        elif event.event_type == "arrival":
            arrivals[event.route_id] = event.timestamp.astimezone(tz)
        elif event.source == Source.SYSTEM and event.auto_code:
            system[(event.route_id, event.auto_code)] = event

    depot = _depot(company)
    stops_by_route = _stops_by_route(list(by_id))
    created = 0
    with transaction.atomic():
        for route in routes:
            arrival_time = datetime.combine(day, route.schedule.time, tzinfo=tz)
            deadline = arrival_time + timedelta(minutes=TOLERANCE_MINUTES)
            departed, arrived = departures.get(route.id), arrivals.get(route.id)

            not_started = system.get((route.id, "not_started"))
            if departed and not_started and not_started.status != Status.RESOLVED:
                _auto_resolve(not_started, f"Partida registrada às {departed:%H:%M}.")
            planned = _planned_departure(arrival_time, stops_by_route.get(route.id, []), depot)
            if planned and not departed and not arrived and not not_started:
                late_by = _minutes(now - planned)
                if late_by > TOLERANCE_MINUTES:
                    _create(route, day, "not_started", late_by, (
                        f"Rota não iniciada: saída prevista às {planned:%H:%M} e nenhuma partida registrada "
                        f"até {now:%H:%M}."
                    ))
                    created += 1

            overdue = system.get((route.id, "overdue"))
            if arrived and overdue and overdue.status != Status.RESOLVED:
                late_by = _minutes(arrived - arrival_time)
                _auto_resolve(overdue, f"Chegada registrada às {arrived:%H:%M} ({late_by} min depois do horário).")
            if not arrived and now > deadline and not overdue:
                _create(route, day, "overdue", _minutes(now - arrival_time), (
                    f"Passou do horário de chegada ({route.schedule.time:%H:%M}) sem chegada registrada."
                ))
                created += 1

            # Se o atraso já tinha virado ocorrência (overdue), ela foi resolvida acima com a hora da chegada.
            if arrived and arrived > deadline and not overdue and (route.id, "late_arrival") not in system:
                late_by = _minutes(arrived - arrival_time)
                _create(route, day, "late_arrival", late_by, (
                    f"Chegou às {arrived:%H:%M}, {late_by} min depois do horário ({route.schedule.time:%H:%M})."
                ))
                created += 1
    return created


def _create(route, day, code, minutes, description):
    event = RouteEvent(
        route=route, event_type="delay", source=Source.SYSTEM, auto_code=code, category=RouteEvent.Category.SCHEDULE,
        service_date=day, delay_minutes=max(minutes, 0), description=description,
    )
    event.notify_passengers = False  # o sistema não manda push; o painel mostra
    event.save()
    return event


def _auto_resolve(event, resolution):
    event.status = Status.RESOLVED
    event.resolved_at = timezone.now()
    event.resolved_by = None
    event.resolution = resolution
    event.save(update_fields=["status", "resolved_at", "resolved_by", "resolution", "updated_at"])


def _minutes(delta):
    return int(delta.total_seconds() // 60)


def _depot(company):
    address = company_location(company)
    if address is None or address.latitude is None:
        return None
    return SimpleNamespace(lat=float(address.latitude), lng=float(address.longitude))


def _stops_by_route(route_ids):
    stops = {}
    for stop in (
        RouteStop.objects.filter(route_id__in=route_ids, is_active=True)
        .select_related("meeting_point__address").order_by("route_id", "sequence")
    ):
        address = stop.meeting_point.address
        if address.latitude is not None:
            point = SimpleNamespace(lat=float(address.latitude), lng=float(address.longitude))
            stops.setdefault(stop.route_id, []).append((point, []))
    return stops


def _planned_departure(arrival_time, stops, depot):
    """Saída prevista = chegada − tempo estimado do primeiro ponto até a empresa − margem."""
    if not stops or depot is None:
        return None
    minutes_before, _ = route_optimizer.route_timeline(stops, depot)
    return arrival_time - timedelta(minutes=minutes_before[0] + DEPARTURE_MARGIN_MINUTES)


# ---------------------------------------------------------------------- ações do gestor

def start(event, person):
    _ensure_occurrence(event)
    if event.status == Status.RESOLVED:
        raise OccurrenceError("Esta ocorrência já foi resolvida; reabra antes.")
    event.status = Status.IN_PROGRESS
    event.save(update_fields=["status", "updated_at"])
    return event


def resolve(event, person, resolution):
    _ensure_occurrence(event)
    resolution = (resolution or "").strip()
    if not resolution:
        raise OccurrenceError("Conte como a ocorrência foi resolvida.")
    event.status = Status.RESOLVED
    event.resolved_at = timezone.now()
    event.resolved_by = person
    event.resolution = resolution
    event.save(update_fields=["status", "resolved_at", "resolved_by", "resolution", "updated_at"])
    return event


def reopen(event, person):
    _ensure_occurrence(event)
    event.status = Status.OPEN
    event.resolved_at = None
    event.resolved_by = None
    event.save(update_fields=["status", "resolved_at", "resolved_by", "updated_at"])
    return event


def _ensure_occurrence(event):
    if not event.is_occurrence:
        raise OccurrenceError("Só atrasos e incidentes têm status; partida, chegada e parada são registros da viagem.")
