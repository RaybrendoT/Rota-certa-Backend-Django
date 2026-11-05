"""
Resumo do dia para a tela inicial do painel: GET dashboard/summary/?date=AAAA-MM-DD.

Rotas do dia = rotas ativas da empresa com service_date igual à data (rotas avulsas) ou
fixas (service_date vazia) cujo turno roda naquele dia da semana. Rota fixa sem turno não
tem dia definido e fica de fora.

O status de cada rota sai dos eventos registrados pelo motorista naquela data (fuso da
operação): sem partida = não iniciada; partida sem chegada = em andamento; chegada = finalizada.

O status de embarque (RoutePassenger.boarding_status) não guarda a data. Só conta como
confirmado/ausente no dia quando foi alterado naquele dia; senão o passageiro está pendente.

Pontualidade: rota finalizada cuja chegada aconteceu até o horário do turno mais a tolerância.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .address_review import review_queryset
from .dates import local_today
from .models import Address, Route, RouteEvent, RoutePassenger, RouteStop, VehicleLocation
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


def routes_of_day(company, day: date):
    weekday = WEEKDAY_CODES[day.weekday()]
    fixed = [
        route
        for route in Route.objects.filter(
            company=company, is_active=True, service_date__isnull=True, schedule__isnull=False, schedule__is_active=True
        ).select_related("schedule", "vehicle__driver")
        if weekday in (route.schedule.weekdays if isinstance(route.schedule.weekdays, list) else [route.schedule.weekdays])
    ]
    extra = list(
        Route.objects.filter(company=company, is_active=True, service_date=day).select_related("schedule", "vehicle__driver")
    )
    return fixed + extra


def dashboard_summary(company, day: date) -> dict:
    tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
    start, end = _local_day_bounds(day)
    routes = routes_of_day(company, day)
    route_ids = [route.id for route in routes]

    departures, arrivals = {}, {}
    delays = incidents = 0
    events = RouteEvent.objects.filter(
        route_id__in=route_ids, is_active=True, timestamp__gte=start, timestamp__lt=end
    ).values_list("route_id", "event_type", "timestamp")
    for route_id, event_type, timestamp in events:
        if event_type == "departure":
            departures.setdefault(route_id, timestamp)
        elif event_type == "arrival":
            arrivals[route_id] = max(arrivals.get(route_id, timestamp), timestamp)
        elif event_type == "delay":
            delays += 1
        elif event_type == "incident":
            incidents += 1

    passengers_by_route = {route_id: {"expected": 0, "confirmed": 0, "absent": 0} for route_id in route_ids}
    passengers = RoutePassenger.objects.filter(
        route_id__in=route_ids, is_active=True, person__is_active=True
    ).values_list("route_id", "boarding_status", "updated_at")
    for route_id, boarding_status, updated_at in passengers:
        counts = passengers_by_route[route_id]
        counts["expected"] += 1
        if start <= updated_at < end and boarding_status in ("confirmed", "absent"):
            counts[boarding_status] += 1

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
        "events": {"delays": delays, "incidents": incidents},
        "occupancy": {"seats": seats, "passengers": seated, "rate": _rate(seated, seats)},
        "punctuality": {
            "finished": on_time + late,
            "on_time": on_time,
            "late": late,
            "rate": _rate(on_time, on_time + late),
            "tolerance_minutes": ON_TIME_TOLERANCE_MINUTES,
        },
        "shifts": sorted(shifts.values(), key=lambda s: (s["time"] is None, s["time"] or "")),
    }


class DashboardSummaryQuery(serializers.Serializer):
    date = serializers.DateField(required=False)


class DashboardSummaryView(APIView):
    """GET dashboard/summary/?date=AAAA-MM-DD — números do dia da empresa do gestor."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        query = DashboardSummaryQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data.get("date") or local_today()
        return Response(dashboard_summary(request.user.person.company, day))


# ---------------------------------------------------------------------------
# Alertas: GET dashboard/alerts/?date=AAAA-MM-DD — o que pede ação do gestor no dia.
#
# - address_reviews: endereços enviados pelos colaboradores aguardando aprovação (não depende do dia);
# - incidents: incidentes registrados nas rotas do dia;
# - delays: rotas com atraso registrado e, só no dia de hoje, rotas que passaram do horário do
#   turno (mais a tolerância) sem chegada registrada;
# - unassigned: rotas do dia sem veículo ou com veículo sem motorista.
# Cada grupo traz o total e todos os itens (o painel mostra os primeiros e expande o resto).
# ---------------------------------------------------------------------------
def _hhmm(value, tz):
    return value.astimezone(tz).strftime("%H:%M")


def _group(kind, items):
    return {"kind": kind, "count": len(items), "items": items}


def dashboard_alerts(company, day: date, now=None) -> dict:
    tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
    now = (now or timezone.now()).astimezone(tz)
    start, end = _local_day_bounds(day)
    routes = routes_of_day(company, day)
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
        .values_list("id", "route_id", "event_type", "description", "timestamp")
    )
    arrived, delayed, incident_items = set(), {}, []
    for event_id, route_id, event_type, description, timestamp in events:
        if event_type == "arrival":
            arrived.add(route_id)
        elif event_type == "delay":
            delayed.setdefault(route_id, (timestamp, description))
        else:
            incident_items.append({
                "id": event_id, "route_id": route_id, "route_name": by_id[route_id].name,
                "time": _hhmm(timestamp, tz), "description": description,
            })

    delay_items = [
        {
            "route_id": route_id, "route_name": by_id[route_id].name, "reason": "reported",
            "time": _hhmm(timestamp, tz), "description": description,
        }
        for route_id, (timestamp, description) in delayed.items()
    ]
    if day == now.date():
        overdue = []
        for route in routes:
            if route.id in arrived or route.id in delayed or not route.schedule_id:
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
        return Response(dashboard_alerts(request.user.person.company, day))


# ---------------------------------------------------------------------------
# Feed: GET dashboard/feed/?date=AAAA-MM-DD — o que aconteceu nas rotas da empresa no dia,
# do mais novo para o mais antigo. Mesmo formato das mensagens route_event e boarding_status
# do WebSocket (/ws/stream/), para o painel abrir preenchido e seguir ao vivo.
# Embarques: como o status não guarda data, entram os passageiros alterados no dia.
# ---------------------------------------------------------------------------
FEED_LIMIT = 40


def dashboard_feed(company, day: date, limit: int = FEED_LIMIT) -> dict:
    start, end = _local_day_bounds(day)
    events = (
        RouteEvent.objects.filter(
            route__company=company, route__is_active=True, is_active=True, timestamp__gte=start, timestamp__lt=end
        )
        .select_related("route")
        .order_by("-timestamp", "-id")[:limit]
    )
    boardings = (
        RoutePassenger.objects.filter(
            route__company=company, route__is_active=True, is_active=True,
            boarding_status__in=("confirmed", "absent"), updated_at__gte=start, updated_at__lt=end,
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
            "type": "boarding_status", "route_passenger_id": passenger.id, "route_id": passenger.route_id,
            "route_name": passenger.route.name, "person_id": passenger.person_id,
            "person_name": passenger.person.name, "status": passenger.boarding_status,
            "timestamp": passenger.updated_at.isoformat(),
        }
        for passenger in boardings
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
        return Response(dashboard_feed(request.user.person.company, day))


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
