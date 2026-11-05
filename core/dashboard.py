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

from .dates import local_today
from .models import Route, RouteEvent, RoutePassenger
from .permissions import IsManager

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
        ).select_related("schedule", "vehicle")
        if weekday in (route.schedule.weekdays if isinstance(route.schedule.weekdays, list) else [route.schedule.weekdays])
    ]
    extra = list(
        Route.objects.filter(company=company, is_active=True, service_date=day).select_related("schedule", "vehicle")
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
