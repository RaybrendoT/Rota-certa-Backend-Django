"""
Resumo do dia para a tela inicial do painel: GET dashboard/summary/?date=AAAA-MM-DD.

Rotas do dia = rotas ativas da empresa com service_date igual à data (rotas avulsas) ou
fixas (service_date vazia) cujo turno roda naquele dia da semana. Rota fixa sem turno não
tem dia definido e fica de fora.

O status de cada rota sai dos eventos registrados pelo motorista naquela data (fuso da
operação): sem partida = não iniciada; partida sem chegada = em andamento; chegada = finalizada.

Embarques e ausências vêm do BoardingRecord do dia (core.boarding): o passageiro sem registro
naquela data está pendente.

Pontualidade: rota finalizada cuja chegada aconteceu até o horário do turno mais a tolerância.
"""
import csv
import io
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .address_review import review_queryset
from .dates import local_today
from .models import (
    Address,
    BoardingRecord,
    Route,
    RouteEvent,
    RoutePassenger,
    RouteStop,
    Schedule,
    VehicleLocation,
)
from .occurrences import detect as detect_occurrences
from .permissions import IsManager
from .realtime_serializers import location_payload

WEEKDAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
ON_TIME_TOLERANCE_MINUTES = 10


def _local_day_bounds(day: date):
    tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return start, start + timedelta(days=1)


def _rate(part, whole):
    return round(part / whole, 4) if whole else None


def routes_of_day(company, day: date, schedule_id: int | None = None):
    """Rotas do dia; com `schedule_id`, só as daquele turno (filtro do painel)."""
    weekday = WEEKDAY_CODES[day.weekday()]
    by_schedule = {"schedule_id": schedule_id} if schedule_id else {}
    fixed = [
        route
        for route in Route.objects.filter(
            company=company, is_active=True, service_date__isnull=True, schedule__isnull=False, schedule__is_active=True,
            **by_schedule,
        ).select_related("schedule", "vehicle__driver")
        if weekday in (route.schedule.weekdays if isinstance(route.schedule.weekdays, list) else [route.schedule.weekdays])
    ]
    extra = list(
        Route.objects.filter(company=company, is_active=True, service_date=day, **by_schedule)
        .select_related("schedule", "vehicle__driver")
    )
    return fixed + extra


def dashboard_summary(company, day: date, schedule_id: int | None = None) -> dict:
    tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
    start, end = _local_day_bounds(day)
    routes = routes_of_day(company, day, schedule_id)
    route_ids = [route.id for route in routes]

    departures, arrivals = {}, {}
    delays = incidents = open_occurrences = 0
    # Ocorrências contam todas as do dia (motorista, gestor e as detectadas pelo sistema).
    events = RouteEvent.objects.filter(
        route_id__in=route_ids, is_active=True, timestamp__gte=start, timestamp__lt=end
    ).values_list("route_id", "event_type", "timestamp", "status")
    for route_id, event_type, timestamp, event_status in events:
        if event_type == "departure":
            departures.setdefault(route_id, timestamp)
        elif event_type == "arrival":
            arrivals[route_id] = max(arrivals.get(route_id, timestamp), timestamp)
        elif event_type in RouteEvent.OCCURRENCE_TYPES:
            if event_type == "delay":
                delays += 1
            else:
                incidents += 1
            open_occurrences += event_status != RouteEvent.Status.RESOLVED

    passengers_by_route = {route_id: {"expected": 0, "confirmed": 0, "absent": 0} for route_id in route_ids}
    passenger_ids = {}
    for passenger_id, route_id in RoutePassenger.objects.filter(
        route_id__in=route_ids, is_active=True, person__is_active=True
    ).values_list("id", "route_id"):
        passenger_ids[passenger_id] = route_id
        passengers_by_route[route_id]["expected"] += 1
    for passenger_id, record_status in BoardingRecord.objects.filter(
        route_passenger_id__in=passenger_ids, service_date=day, status__in=("confirmed", "absent")
    ).values_list("route_passenger_id", "status"):
        passengers_by_route[passenger_ids[passenger_id]][record_status] += 1

    shifts = {}
    totals = {"not_started": 0, "in_progress": 0, "finished": 0, "without_vehicle": 0}
    seats = seated = 0
    on_time = late = 0
    for route in routes:
        if route.id in arrivals:
            state = "finished"
        elif route.id in departures:
            state = "in_progress"
        else:
            state = "not_started"
        totals[state] += 1
        counts = passengers_by_route[route.id]
        capacity = route.vehicle.capacity if route.vehicle_id and route.vehicle.is_active else 0
        if not capacity:
            totals["without_vehicle"] += 1
        else:
            seats += capacity
            seated += counts["expected"]

        if state == "finished" and route.schedule_id:
            arrived = arrivals[route.id].astimezone(tz)
            deadline = datetime.combine(day, route.schedule.time, tzinfo=tz) + timedelta(
                minutes=ON_TIME_TOLERANCE_MINUTES
            )
            if arrived <= deadline:
                on_time += 1
            else:
                late += 1

        key = route.schedule_id
        shift = shifts.setdefault(key, {
            "schedule_id": key,
            "time": route.schedule.time.strftime("%H:%M") if route.schedule_id else None,
            "routes": 0, "in_progress": 0, "finished": 0,
            "passengers": 0, "confirmed": 0, "absent": 0, "seats": 0,
        })
        shift["routes"] += 1
        shift["in_progress"] += state == "in_progress"
        shift["finished"] += state == "finished"
        shift["passengers"] += counts["expected"]
        shift["confirmed"] += counts["confirmed"]
        shift["absent"] += counts["absent"]
        shift["seats"] += capacity

    for shift in shifts.values():
        shift["occupancy_rate"] = _rate(shift["passengers"], shift["seats"])

    expected = sum(c["expected"] for c in passengers_by_route.values())
    confirmed = sum(c["confirmed"] for c in passengers_by_route.values())
    absent = sum(c["absent"] for c in passengers_by_route.values())
    return {
        "date": day.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "routes": {"total": len(routes), **totals},
        "passengers": {
            "expected": expected,
            "confirmed": confirmed,
            "absent": absent,
            "pending": expected - confirmed - absent,
        },
        "events": {"delays": delays, "incidents": incidents, "open": open_occurrences},
        "occupancy": {"seats": seats, "passengers": seated, "rate": _rate(seated, seats)},
        "punctuality": {
            "finished": on_time + late,
            "on_time": on_time,
            "late": late,
            "rate": _rate(on_time, on_time + late),
            "tolerance_minutes": ON_TIME_TOLERANCE_MINUTES,
        },
        "shifts": sorted(shifts.values(), key=lambda s: (s["time"] is None, s["time"] or "")),
        # Turnos que rodam no dia (para o filtro do painel), independentemente do filtro aplicado.
        "available_shifts": available_shifts(company, day),
        "route_ids": route_ids,
    }


def available_shifts(company, day: date) -> list[dict]:
    weekday = WEEKDAY_CODES[day.weekday()]
    return [
        {"id": schedule.id, "time": schedule.time.strftime("%H:%M")}
        for schedule in Schedule.objects.filter(company=company, is_active=True).order_by("time", "id")
        if weekday in (schedule.weekdays if isinstance(schedule.weekdays, list) else [schedule.weekdays])
        and schedule.routes.filter(is_active=True).exists()
    ]


class DashboardSummaryQuery(serializers.Serializer):
    date = serializers.DateField(required=False)
    schedule = serializers.IntegerField(required=False, min_value=1, help_text="Só as rotas deste turno.")


class DashboardSummaryView(APIView):
    """GET dashboard/summary/?date=AAAA-MM-DD — números do dia da empresa do gestor."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = DashboardSummaryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or local_today()
        company = request.user.person.company
        if day == local_today():
            detect_occurrences(company)
        return Response(dashboard_summary(company, day, query.validated_data.get("schedule")))


# ---------------------------------------------------------------------------
# Alertas: GET dashboard/alerts/?date=AAAA-MM-DD — o que pede ação do gestor no dia.
#
# - address_reviews: endereços enviados pelos colaboradores aguardando aprovação (não depende do dia);
# - incidents: incidentes ainda não resolvidos nas rotas do dia;
# - delays: atrasos ainda não resolvidos (do motorista, do gestor ou detectados pelo sistema; ver
#   core/occurrences.py) e, só no dia de hoje, rotas que passaram do horário do turno (mais a
#   tolerância) sem chegada nem ocorrência registrada;
# - unassigned: rotas do dia sem veículo ou com veículo sem motorista.
# Cada grupo traz o total e todos os itens (o painel mostra os primeiros e expande o resto).
# ---------------------------------------------------------------------------
def _hhmm(value, tz):
    return value.astimezone(tz).strftime("%H:%M")


def _group(kind, items):
    return {"kind": kind, "count": len(items), "items": items}


def dashboard_alerts(company, day: date, now=None, schedule_id: int | None = None) -> dict:
    tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
    now = (now or timezone.now()).astimezone(tz)
    start, end = _local_day_bounds(day)
    routes = routes_of_day(company, day, schedule_id)
    by_id = {route.id: route for route in routes}

    pending = review_queryset(company).filter(review_status=Address.Review.PENDING).order_by("created_at", "id")
    address_items = [
        {"id": address.id, "person_name": address.person.name, "time": address.created_at.isoformat()}
        for address in pending
    ]

    events = (
        RouteEvent.objects.filter(
            route_id__in=by_id, is_active=True, timestamp__gte=start, timestamp__lt=end,
            event_type__in=("arrival", "delay", "incident"),
        )
        .order_by("timestamp", "id")
        .values_list("id", "route_id", "event_type", "description", "timestamp", "status", "auto_code", "source")
    )
    arrived, delayed, any_delay, incident_items = set(), {}, set(), []
    for event_id, route_id, event_type, description, timestamp, event_status, auto_code, source in events:
        if event_type == "arrival":
            arrived.add(route_id)
            continue
        if event_type == "delay":
            any_delay.add(route_id)
        if event_status == RouteEvent.Status.RESOLVED:
            continue
        if event_type == "delay":
            delayed.setdefault(route_id, (event_id, timestamp, description, auto_code, source, event_status))
        else:
            incident_items.append({
                "id": event_id, "route_id": route_id, "route_name": by_id[route_id].name,
                "time": _hhmm(timestamp, tz), "description": description, "source": source, "status": event_status,
            })

    delay_items = [
        {
            "id": event_id, "route_id": route_id, "route_name": by_id[route_id].name,
            # not_started / overdue / late_arrival (sistema) ou reported (motorista ou gestor).
            "reason": auto_code or "reported",
            "time": by_id[route_id].schedule.time.strftime("%H:%M") if auto_code and by_id[route_id].schedule_id
            else _hhmm(timestamp, tz),
            "description": description, "source": source, "status": event_status,
        }
        for route_id, (event_id, timestamp, description, auto_code, source, event_status) in delayed.items()
    ]
    if day == now.date():
        overdue = []
        for route in routes:
            if route.id in arrived or route.id in any_delay or not route.schedule_id:
                continue
            deadline = datetime.combine(day, route.schedule.time, tzinfo=tz) + timedelta(
                minutes=ON_TIME_TOLERANCE_MINUTES
            )
            if now > deadline:
                overdue.append((route.schedule.time, route.name, {
                    "route_id": route.id, "route_name": route.name, "reason": "overdue",
                    "time": route.schedule.time.strftime("%H:%M"), "description": "",
                }))
        delay_items += [item for _, _, item in sorted(overdue, key=lambda x: (x[0], x[1]))]

    unassigned_items = []
    for route in sorted(routes, key=lambda r: (r.schedule.time if r.schedule_id else datetime.max.time(), r.name)):
        vehicle = route.vehicle if route.vehicle_id and route.vehicle.is_active else None
        if vehicle is None:
            reason = "no_vehicle"
        elif vehicle.driver is None or not vehicle.driver.is_active:
            reason = "no_driver"
        else:
            continue
        unassigned_items.append({
            "route_id": route.id, "route_name": route.name, "reason": reason,
            "time": route.schedule.time.strftime("%H:%M") if route.schedule_id else None,
        })

    groups = [
        _group("incidents", incident_items),
        _group("delays", delay_items),
        _group("unassigned", unassigned_items),
        _group("address_reviews", address_items),
    ]
    return {
        "date": day.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "total": sum(group["count"] for group in groups),
        "groups": groups,
    }


class DashboardAlertsView(APIView):
    """GET dashboard/alerts/?date=AAAA-MM-DD — pendências que pedem ação do gestor."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = DashboardSummaryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or local_today()
        company = request.user.person.company
        if day == local_today():
            detect_occurrences(company)
        return Response(dashboard_alerts(company, day, schedule_id=query.validated_data.get("schedule")))


# ---------------------------------------------------------------------------
# Feed: GET dashboard/feed/?date=AAAA-MM-DD — o que aconteceu nas rotas da empresa no dia,
# do mais novo para o mais antigo. Mesmo formato das mensagens route_event e boarding_status
# do WebSocket (/ws/stream/), para o painel abrir preenchido e seguir ao vivo.
# Embarques: os BoardingRecord do dia (último status de cada passageiro naquela data).
# ---------------------------------------------------------------------------
FEED_LIMIT = 40


def dashboard_feed(company, day: date, limit: int = FEED_LIMIT, schedule_id: int | None = None) -> dict:
    start, end = _local_day_bounds(day)
    by_schedule = {"route__schedule_id": schedule_id} if schedule_id else {}
    events = (
        RouteEvent.objects.filter(
            route__company=company, route__is_active=True, is_active=True, timestamp__gte=start, timestamp__lt=end,
            **by_schedule,
        )
        .select_related("route")
        .order_by("-timestamp", "-id")[:limit]
    )
    boardings = (
        BoardingRecord.objects.filter(
            route__company=company, route__is_active=True, route_passenger__is_active=True,
            service_date=day, status__in=("confirmed", "absent"), **by_schedule,
        )
        .select_related("route", "person")
        .order_by("-updated_at", "-id")[:limit]
    )
    items = [
        {
            "type": "route_event", "id": event.id, "route_id": event.route_id, "route_name": event.route.name,
            "event_type": event.event_type, "description": event.description,
            "timestamp": event.timestamp.isoformat(),
        }
        for event in events
    ] + [
        {
            "type": "boarding_status", "route_passenger_id": record.route_passenger_id, "route_id": record.route_id,
            "route_name": record.route.name, "person_id": record.person_id,
            "person_name": record.person.name, "status": record.status,
            "timestamp": record.updated_at.isoformat(),
        }
        for record in boardings
    ]
    items.sort(key=lambda item: item["timestamp"], reverse=True)
    return {"date": day.isoformat(), "items": items[:limit]}


class DashboardFeedView(APIView):
    """GET dashboard/feed/?date=AAAA-MM-DD — últimos eventos e embarques do dia."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = DashboardSummaryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or local_today()
        return Response(
            dashboard_feed(request.user.person.company, day, schedule_id=query.validated_data.get("schedule"))
        )


# ---------------------------------------------------------------------------
# Mapa ao vivo: GET dashboard/map/?date=AAAA-MM-DD — rotas do dia com o ônibus, o status, as
# paradas e o destino (endereço da empresa). A posição só vem se o motorista a enviou naquele
# dia; depois o painel segue pelas mensagens vehicle_location do WebSocket.
# ---------------------------------------------------------------------------
STALE_LOCATION_SECONDS = 120


def _point(latitude, longitude):
    return {"latitude": float(latitude), "longitude": float(longitude)}


def dashboard_map(company, day: date) -> dict:
    start, end = _local_day_bounds(day)
    routes = routes_of_day(company, day)
    route_ids = [route.id for route in routes]

    states = {}
    for route_id, event_type in RouteEvent.objects.filter(
        route_id__in=route_ids, is_active=True, timestamp__gte=start, timestamp__lt=end,
        event_type__in=("departure", "arrival"),
    ).values_list("route_id", "event_type"):
        if event_type == "arrival" or states.get(route_id) != "finished":
            states[route_id] = "finished" if event_type == "arrival" else "in_progress"
    delayed = {
        item["route_id"]
        for group in dashboard_alerts(company, day)["groups"] if group["kind"] == "delays"
        for item in group["items"]
    }
    locations = {
        location.route_id: location
        for location in VehicleLocation.objects.filter(
            route_id__in=route_ids, recorded_at__gte=start, recorded_at__lt=end
        )
    }
    stops = {}
    for stop in (
        RouteStop.objects.filter(route_id__in=route_ids, is_active=True, meeting_point__is_active=True)
        .select_related("meeting_point__address")
        .order_by("route_id", "sequence")
    ):
        address = stop.meeting_point.address
        stops.setdefault(stop.route_id, []).append(
            {"sequence": stop.sequence, "name": stop.meeting_point.name, **_point(address.latitude, address.longitude)}
        )
    passengers = {}
    for route_id in RoutePassenger.objects.filter(
        route_id__in=route_ids, is_active=True, person__is_active=True
    ).values_list("route_id", flat=True):
        passengers[route_id] = passengers.get(route_id, 0) + 1

    destination = (
        # Endereço da própria empresa: sem pessoa e fora dos pontos de encontro (que também são da empresa).
        Address.objects.filter(company=company, person__isnull=True, is_active=True, meeting_points__isnull=True)
        .order_by("id")
        .first()
    )
    items = []
    for route in sorted(routes, key=lambda r: (r.schedule.time if r.schedule_id else datetime.max.time(), r.name)):
        vehicle = route.vehicle if route.vehicle_id and route.vehicle.is_active else None
        driver = vehicle.driver if vehicle and vehicle.driver_id and vehicle.driver.is_active else None
        location = locations.get(route.id)
        items.append({
            "id": route.id,
            "name": route.name,
            "time": route.schedule.time.strftime("%H:%M") if route.schedule_id else None,
            "state": states.get(route.id, "not_started"),
            "delayed": route.id in delayed,
            "vehicle": (
                {"plate": vehicle.plate_number, "model": vehicle.model, "capacity": vehicle.capacity} if vehicle else None
            ),
            "driver_name": driver.name if driver else None,
            "passengers": passengers.get(route.id, 0),
            "location": location_payload(location) if location else None,
            "stops": stops.get(route.id, []),
        })
    return {
        "date": day.isoformat(),
        "generated_at": timezone.now().isoformat(),
        "stale_after_seconds": STALE_LOCATION_SECONDS,
        "destination": (
            {"name": company.name, **_point(destination.latitude, destination.longitude)} if destination else None
        ),
        "routes": items,
    }


class DashboardMapView(APIView):
    """GET dashboard/map/?date=AAAA-MM-DD — rotas do dia com posição, status e paradas."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = DashboardSummaryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or local_today()
        return Response(dashboard_map(request.user.person.company, day))


# ---------------------------------------------------------------------------
# Tendências: GET dashboard/trends/?days=7|30 — os últimos N dias até hoje.
# - daily: por dia, previstos/embarques/ausências (BoardingRecord), rotas e pontualidade;
# - routes: rotas com mais ausências no período (taxa = ausências ÷ passageiros-dia previstos).
# Os previstos de cada dia usam a lista atual de passageiros das rotas daquele dia.
# ---------------------------------------------------------------------------
TREND_DAYS = (7, 14, 30)
TREND_TOP_ROUTES = 8


def dashboard_trends(company, days: int, today: date | None = None) -> dict:
    today = today or local_today()
    first = today - timedelta(days=days - 1)
    daily = []
    by_route = {}
    roster = dict(
        RoutePassenger.objects.filter(route__company=company, is_active=True, person__is_active=True)
        .values_list("route_id")
        .annotate(n=models.Count("id"))
    )
    for offset in range(days):
        day = first + timedelta(days=offset)
        summary = dashboard_summary(company, day)
        daily.append({
            "date": day.isoformat(),
            "routes": summary["routes"]["total"],
            "finished": summary["routes"]["finished"],
            "expected": summary["passengers"]["expected"],
            "confirmed": summary["passengers"]["confirmed"],
            "absent": summary["passengers"]["absent"],
            "pending": summary["passengers"]["pending"],
            "on_time": summary["punctuality"]["on_time"],
            "late": summary["punctuality"]["late"],
            "punctuality_rate": summary["punctuality"]["rate"],
            "delays": summary["events"]["delays"],
            "incidents": summary["events"]["incidents"],
        })
        for route in routes_of_day(company, day):
            entry = by_route.setdefault(route.id, {"route_id": route.id, "name": route.name, "expected": 0, "absent": 0})
            entry["expected"] += roster.get(route.id, 0)

    for route_id, absent in (
        BoardingRecord.objects.filter(
            route_id__in=by_route, service_date__gte=first, service_date__lte=today, status="absent",
            route_passenger__is_active=True,
        )
        .values_list("route_id")
        .annotate(n=models.Count("id"))
    ):
        by_route[route_id]["absent"] = absent
    ranking = [
        {**entry, "rate": _rate(entry["absent"], entry["expected"])}
        for entry in by_route.values()
        if entry["absent"]
    ]
    ranking.sort(key=lambda e: (-(e["rate"] or 0), -e["absent"], e["name"]))
    return {"days": days, "from": first.isoformat(), "to": today.isoformat(), "daily": daily,
            "routes": ranking[:TREND_TOP_ROUTES]}


class DashboardTrendsQuery(serializers.Serializer):
    days = serializers.ChoiceField(choices=TREND_DAYS, default=7)


class DashboardTrendsView(APIView):
    """GET dashboard/trends/?days=7|14|30 — embarques, ausências e pontualidade dos últimos dias."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = DashboardTrendsQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(dashboard_trends(request.user.person.company, int(query.validated_data["days"])))


# ---------------------------------------------------------------------------
# Exportar: GET dashboard/export/?date=AAAA-MM-DD&schedule=<id> — CSV com uma linha por rota do dia,
# separado por ";" e com BOM UTF-8, para abrir direto no Excel em português.
# ---------------------------------------------------------------------------
EXPORT_STATES = {"not_started": "Não iniciada", "in_progress": "Em andamento", "finished": "Finalizada"}


def dashboard_export_rows(company, day: date, schedule_id: int | None = None) -> list[list]:
    tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
    start, end = _local_day_bounds(day)
    routes = routes_of_day(company, day, schedule_id)
    route_ids = [route.id for route in routes]
    departures, arrivals, delays, incidents = {}, {}, {}, {}
    for route_id, event_type, timestamp in RouteEvent.objects.filter(
        route_id__in=route_ids, is_active=True, timestamp__gte=start, timestamp__lt=end
    ).order_by("timestamp").values_list("route_id", "event_type", "timestamp"):
        if event_type == "departure":
            departures.setdefault(route_id, timestamp)
        elif event_type == "arrival":
            arrivals[route_id] = timestamp
        elif event_type == "delay":
            delays[route_id] = delays.get(route_id, 0) + 1
        elif event_type == "incident":
            incidents[route_id] = incidents.get(route_id, 0) + 1
    expected = dict(
        RoutePassenger.objects.filter(route_id__in=route_ids, is_active=True, person__is_active=True)
        .values_list("route_id").annotate(n=models.Count("id"))
    )
    boarded = {}
    for route_id, status, n in (
        BoardingRecord.objects.filter(route_id__in=route_ids, service_date=day, route_passenger__is_active=True)
        .values_list("route_id", "status").annotate(n=models.Count("id"))
    ):
        boarded[(route_id, status)] = n

    def hhmm(value):
        return value.astimezone(tz).strftime("%H:%M") if value else ""

    rows = [[
        "Data", "Turno", "Rota", "Status", "Partida", "Chegada", "Pontual", "Veículo", "Motorista",
        "Capacidade", "Passageiros", "Embarques", "Ausências", "Sem registro", "Atrasos", "Incidentes",
    ]]
    for route in sorted(routes, key=lambda r: (r.schedule.time if r.schedule_id else datetime.max.time(), r.name)):
        vehicle = route.vehicle if route.vehicle_id and route.vehicle.is_active else None
        driver = vehicle.driver if vehicle and vehicle.driver_id else None
        state = "finished" if route.id in arrivals else "in_progress" if route.id in departures else "not_started"
        on_time = ""
        if route.id in arrivals and route.schedule_id:
            deadline = datetime.combine(day, route.schedule.time, tzinfo=tz) + timedelta(minutes=ON_TIME_TOLERANCE_MINUTES)
            on_time = "Sim" if arrivals[route.id].astimezone(tz) <= deadline else "Não"
        total = expected.get(route.id, 0)
        confirmed = boarded.get((route.id, "confirmed"), 0)
        absent = boarded.get((route.id, "absent"), 0)
        rows.append([
            day.strftime("%d/%m/%Y"), route.schedule.time.strftime("%H:%M") if route.schedule_id else "Avulsa",
            route.name, EXPORT_STATES[state], hhmm(departures.get(route.id)), hhmm(arrivals.get(route.id)), on_time,
            vehicle.plate_number if vehicle else "", driver.name if driver else "", vehicle.capacity if vehicle else "",
            total, confirmed, absent, max(0, total - confirmed - absent),
            delays.get(route.id, 0), incidents.get(route.id, 0),
        ])
    return rows


class DashboardExportView(APIView):
    """GET dashboard/export/?date=AAAA-MM-DD&schedule=<id> — rotas do dia em CSV (Excel pt-BR)."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = DashboardSummaryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or local_today()
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerows(dashboard_export_rows(request.user.person.company, day, query.validated_data.get("schedule")))
        response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="rotacerta-resumo-{day.isoformat()}.csv"'
        return response
