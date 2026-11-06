"""
Relatórios do período (gestor, empresa própria): GET reports/<aba>/?from=AAAA-MM-DD&to=AAAA-MM-DD.

Abas:
- attendance: frequência por colaborador (dias previstos, embarques, ausências, presença);
  attendance/<person_id>/ traz a linha do tempo (timeline) do colaborador, dia a dia;
- routes: operação por rota (viagens, pontualidade, atraso médio, ocupação, ocorrências);
- drivers: desempenho por motorista e veículo;
- events: lista de atrasos e incidentes.

Filtros comuns: schedule (turno), route, driver; período de até MAX_DAYS (padrão: últimos 30 dias).
?export=csv devolve a mesma tabela em CSV (";" e BOM UTF-8, para o Excel em português).

"Previsto" = o passageiro estava na lista (atual) de uma rota que rodava naquele dia. Embarques e
ausências vêm do BoardingRecord; viagens, pontualidade e ocorrências, dos eventos do motorista.
"""
import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.http import HttpResponse
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .dashboard import ON_TIME_TOLERANCE_MINUTES, _local_day_bounds, _rate, routes_of_day
from .dates import local_today
from .models import BoardingRecord, Person, Route, RouteEvent, RoutePassenger, Schedule
from .permissions import IsManager

MAX_DAYS = 90
DEFAULT_DAYS = 30
EVENTS_LIMIT = 2000


# ---------------------------------------------------------------------------
# Período e filtros
# ---------------------------------------------------------------------------
class ReportQuery(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    schedule = serializers.IntegerField(required=False, min_value=1)
    route = serializers.IntegerField(required=False, min_value=1)
    driver = serializers.IntegerField(required=False, min_value=1)
    type = serializers.ChoiceField(choices=("delay", "incident"), required=False)
    export = serializers.ChoiceField(choices=("csv",), required=False)

    def to_internal_value(self, data):
        # "from"/"to" na URL (palavras reservadas em Python): renomeia antes de validar.
        data = {**{k: v for k, v in data.items()}, "date_from": data.get("from"), "date_to": data.get("to")}
        data = {k: v for k, v in data.items() if v not in (None, "")}
        return super().to_internal_value(data)

    def validate(self, attrs):
        today = local_today()
        end = attrs.get("date_to") or today
        start = attrs.get("date_from") or end - timedelta(days=DEFAULT_DAYS - 1)
        if start > end:
            raise serializers.ValidationError({"from": "A data inicial é depois da final."})
        if (end - start).days + 1 > MAX_DAYS:
            raise serializers.ValidationError({"from": f"O período pode ter no máximo {MAX_DAYS} dias."})
        attrs["date_from"], attrs["date_to"] = start, end
        return attrs


class Period:
    """Dias do período com as rotas que rodaram em cada um (já com os filtros de turno/rota/motorista)."""

    def __init__(self, company, filters):
        self.company = company
        self.start = filters["date_from"]
        self.end = filters["date_to"]
        self.tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
        self.days = []
        route_id, driver_id = filters.get("route"), filters.get("driver")
        day = self.start
        while day <= self.end:
            routes = [
                r for r in routes_of_day(company, day, filters.get("schedule"))
                if (not route_id or r.id == route_id)
                and (not driver_id or (r.vehicle_id and r.vehicle.driver_id == driver_id))
            ]
            self.days.append((day, routes))
            day += timedelta(days=1)
        self.routes = {r.id: r for _, routes in self.days for r in routes}

    @property
    def bounds(self):
        return _local_day_bounds(self.start)[0], _local_day_bounds(self.end)[1]

    def events(self, types):
        start, end = self.bounds
        return RouteEvent.objects.filter(
            route_id__in=self.routes, is_active=True, timestamp__gte=start, timestamp__lt=end, event_type__in=types
        )

    def local_date(self, value):
        return value.astimezone(self.tz).date()

    def roster(self):
        """route_id -> [(route_passenger_id, person_id)] dos passageiros ativos."""
        result = defaultdict(list)
        for rp_id, route_id, person_id in RoutePassenger.objects.filter(
            route_id__in=self.routes, is_active=True, person__is_active=True
        ).values_list("id", "route_id", "person_id"):
            result[route_id].append((rp_id, person_id))
        return result

    def boardings(self):
        """(route_passenger_id, data) -> status."""
        return {
            (rp_id, day): status
            for rp_id, day, status in BoardingRecord.objects.filter(
                route_id__in=self.routes, service_date__gte=self.start, service_date__lte=self.end,
                status__in=("confirmed", "absent"),
            ).values_list("route_passenger_id", "service_date", "status")
        }

    def payload(self):
        return {"from": self.start.isoformat(), "to": self.end.isoformat(), "days": len(self.days)}


def _trips(period):
    """(route_id, dia) -> {"departure", "arrival", "delays", "incidents"} a partir dos eventos."""
    trips = defaultdict(lambda: {"departure": None, "arrival": None, "delays": 0, "incidents": 0})
    for route_id, event_type, timestamp in period.events(("departure", "arrival", "delay", "incident")).values_list(
        "route_id", "event_type", "timestamp"
    ):
        trip = trips[(route_id, period.local_date(timestamp))]
        if event_type == "departure":
            trip["departure"] = min(filter(None, (trip["departure"], timestamp)))
        elif event_type == "arrival":
            trip["arrival"] = max(filter(None, (trip["arrival"], timestamp)))
        elif event_type == "delay":
            trip["delays"] += 1
        else:
            trip["incidents"] += 1
    return trips


def _lateness_minutes(period, route, day, arrival):
    """Minutos de atraso da chegada em relação ao horário do turno (0 se chegou antes)."""
    scheduled = datetime.combine(day, route.schedule.time, tzinfo=period.tz)
    return max(0.0, (arrival.astimezone(period.tz) - scheduled).total_seconds() / 60)


# ---------------------------------------------------------------------------
# Abas
# ---------------------------------------------------------------------------
def attendance_report(company, filters) -> dict:
    period = Period(company, filters)
    roster, boardings = period.roster(), period.boardings()
    people = defaultdict(lambda: {"expected": 0, "confirmed": 0, "absent": 0, "routes": set()})
    for day, routes in period.days:
        for route in routes:
            for rp_id, person_id in roster[route.id]:
                row = people[person_id]
                row["expected"] += 1
                row["routes"].add(route.name)
                status = boardings.get((rp_id, day))
                if status:
                    row[status] += 1
    names = {p.id: p for p in Person.objects.filter(id__in=people).only("id", "name", "employee_code")}
    rows = []
    for person_id, row in people.items():
        person = names[person_id]
        rows.append({
            "person_id": person_id, "name": person.name, "code": person.employee_code or "",
            "routes": sorted(row["routes"]), "expected": row["expected"], "confirmed": row["confirmed"],
            "absent": row["absent"], "no_record": row["expected"] - row["confirmed"] - row["absent"],
            "presence_rate": _rate(row["confirmed"], row["expected"]),
            "absence_rate": _rate(row["absent"], row["expected"]),
        })
    rows.sort(key=lambda r: (-(r["absence_rate"] or 0), r["name"]))
    expected = sum(r["expected"] for r in rows)
    confirmed = sum(r["confirmed"] for r in rows)
    absent = sum(r["absent"] for r in rows)
    return {
        **period.payload(),
        "totals": {
            "people": len(rows), "expected": expected, "confirmed": confirmed, "absent": absent,
            "presence_rate": _rate(confirmed, expected), "absence_rate": _rate(absent, expected),
            "never_boarded": sum(1 for r in rows if r["expected"] and not r["confirmed"]),
        },
        "rows": rows,
    }


def attendance_detail(company, filters, person_id) -> dict | None:
    person = Person.objects.filter(company=company, pk=person_id).first()
    if person is None:
        return None
    period = Period(company, filters)
    passengers = {
        rp.route_id: rp.id
        for rp in RoutePassenger.objects.filter(person=person, route_id__in=period.routes, is_active=True)
    }
    statuses = {
        (rp_id, day): status
        for rp_id, day, status in BoardingRecord.objects.filter(
            route_passenger_id__in=passengers.values(), service_date__gte=period.start, service_date__lte=period.end,
        ).values_list("route_passenger_id", "service_date", "status")
    }
    timeline = [
        {
            "date": day.isoformat(), "route_id": route.id, "route_name": route.name,
            "time": route.schedule.time.strftime("%H:%M") if route.schedule_id else None,
            "status": statuses.get((passengers[route.id], day)) or "no_record",
        }
        for day, routes in period.days
        for route in routes
        if route.id in passengers
    ]
    return {**period.payload(), "person": {"id": person.id, "name": person.name, "code": person.employee_code or ""},
            "timeline": timeline}


def routes_report(company, filters) -> dict:
    period = Period(company, filters)
    roster, boardings, trips = period.roster(), period.boardings(), _trips(period)
    stats = {}
    for day, routes in period.days:
        for route in routes:
            row = stats.setdefault(route.id, {
                "route": route, "planned": 0, "done": 0, "on_time": 0, "late": 0, "late_minutes": [],
                "occupancy": [], "confirmed": 0, "absent": 0, "delays": 0, "incidents": 0,
            })
            row["planned"] += 1
            trip = trips.get((route.id, day))
            if trip:
                row["delays"] += trip["delays"]
                row["incidents"] += trip["incidents"]
            confirmed = sum(1 for rp_id, _ in roster[route.id] if boardings.get((rp_id, day)) == "confirmed")
            row["confirmed"] += confirmed
            row["absent"] += sum(1 for rp_id, _ in roster[route.id] if boardings.get((rp_id, day)) == "absent")
            if not trip or not trip["departure"]:
                continue
            row["done"] += 1
            capacity = route.vehicle.capacity if route.vehicle_id else 0
            if capacity:
                row["occupancy"].append(confirmed / capacity)
            if trip["arrival"] and route.schedule_id:
                minutes = _lateness_minutes(period, route, day, trip["arrival"])
                row["late_minutes"].append(minutes)
                row["on_time" if minutes <= ON_TIME_TOLERANCE_MINUTES else "late"] += 1
    rows = []
    for row in stats.values():
        route = row["route"]
        vehicle = route.vehicle if route.vehicle_id else None
        finished = row["on_time"] + row["late"]
        rows.append({
            "route_id": route.id, "name": route.name,
            "time": route.schedule.time.strftime("%H:%M") if route.schedule_id else None,
            "vehicle": vehicle.plate_number if vehicle else None,
            "driver": vehicle.driver.name if vehicle and vehicle.driver_id else None,
            "capacity": vehicle.capacity if vehicle else None,
            "passengers": len(roster[route.id]),
            "planned": row["planned"], "done": row["done"], "completion_rate": _rate(row["done"], row["planned"]),
            "on_time": row["on_time"], "late": row["late"], "punctuality_rate": _rate(row["on_time"], finished),
            "avg_late_minutes": round(sum(row["late_minutes"]) / len(row["late_minutes"]), 1) if row["late_minutes"] else None,
            "avg_occupancy": round(sum(row["occupancy"]) / len(row["occupancy"]), 4) if row["occupancy"] else None,
            "confirmed": row["confirmed"], "absent": row["absent"],
            "delays": row["delays"], "incidents": row["incidents"],
        })
    rows.sort(key=lambda r: (r["time"] or "99", r["name"]))
    planned = sum(r["planned"] for r in rows)
    done = sum(r["done"] for r in rows)
    on_time = sum(r["on_time"] for r in rows)
    finished = on_time + sum(r["late"] for r in rows)
    occupancies = [r["avg_occupancy"] for r in rows if r["avg_occupancy"] is not None]
    return {
        **period.payload(),
        "totals": {
            "routes": len(rows), "planned": planned, "done": done, "completion_rate": _rate(done, planned),
            "punctuality_rate": _rate(on_time, finished),
            "avg_occupancy": round(sum(occupancies) / len(occupancies), 4) if occupancies else None,
            "delays": sum(r["delays"] for r in rows), "incidents": sum(r["incidents"] for r in rows),
        },
        "rows": rows,
    }


def drivers_report(company, filters) -> dict:
    by_route = routes_report(company, filters)
    drivers = {}
    for row in by_route["rows"]:
        key = (row["driver"] or "Sem motorista", row["vehicle"] or "Sem veículo")
        agg = drivers.setdefault(key, {
            "driver": key[0], "vehicle": key[1], "routes": 0, "planned": 0, "done": 0, "on_time": 0, "late": 0,
            "late_weighted": 0.0, "late_count": 0, "delays": 0, "incidents": 0,
        })
        agg["routes"] += 1
        for field in ("planned", "done", "on_time", "late", "delays", "incidents"):
            agg[field] += row[field]
        finished = row["on_time"] + row["late"]
        if row["avg_late_minutes"] is not None and finished:
            agg["late_weighted"] += row["avg_late_minutes"] * finished
            agg["late_count"] += finished
    rows = []
    for agg in drivers.values():
        finished = agg["on_time"] + agg["late"]
        rows.append({
            "driver": agg["driver"], "vehicle": agg["vehicle"], "routes": agg["routes"],
            "planned": agg["planned"], "done": agg["done"], "completion_rate": _rate(agg["done"], agg["planned"]),
            "on_time": agg["on_time"], "late": agg["late"], "punctuality_rate": _rate(agg["on_time"], finished),
            "avg_late_minutes": round(agg["late_weighted"] / agg["late_count"], 1) if agg["late_count"] else None,
            "delays": agg["delays"], "incidents": agg["incidents"],
        })
    rows.sort(key=lambda r: ((r["punctuality_rate"] if r["punctuality_rate"] is not None else 2), r["driver"]))
    return {**{k: by_route[k] for k in ("from", "to", "days")}, "totals": {"drivers": len(rows)}, "rows": rows}


def events_report(company, filters) -> dict:
    period = Period(company, filters)
    types = (filters["type"],) if filters.get("type") else ("delay", "incident")
    events = list(
        period.events(types).select_related("route__vehicle__driver", "route__schedule")
        .order_by("-timestamp", "-id")[: EVENTS_LIMIT + 1]
    )
    rows = []
    for event in events[:EVENTS_LIMIT]:
        route = event.route
        vehicle = route.vehicle if route.vehicle_id else None
        local = event.timestamp.astimezone(period.tz)
        rows.append({
            "id": event.id, "date": local.date().isoformat(), "time": local.strftime("%H:%M"),
            "type": event.event_type, "route_id": route.id, "route_name": route.name,
            "shift": route.schedule.time.strftime("%H:%M") if route.schedule_id else None,
            "driver": vehicle.driver.name if vehicle and vehicle.driver_id else None,
            "vehicle": vehicle.plate_number if vehicle else None,
            "description": event.description,
        })
    return {
        **period.payload(),
        "totals": {
            "events": len(rows), "delays": sum(1 for r in rows if r["type"] == "delay"),
            "incidents": sum(1 for r in rows if r["type"] == "incident"),
            "truncated": len(events) > EVENTS_LIMIT,
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _pct(rate):
    return "" if rate is None else f"{rate * 100:.1f}".replace(".", ",") + "%"


def _num(value):
    return "" if value is None else str(value).replace(".", ",")


def _br(iso):
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


CSV_LAYOUTS = {
    "attendance": (
        ["Colaborador", "Matrícula", "Rotas", "Dias previstos", "Embarques", "Ausências", "Sem registro",
         "Presença", "Ausência"],
        lambda r: [r["name"], r["code"], ", ".join(r["routes"]), r["expected"], r["confirmed"], r["absent"],
                   r["no_record"], _pct(r["presence_rate"]), _pct(r["absence_rate"])],
    ),
    "routes": (
        ["Rota", "Turno", "Veículo", "Motorista", "Capacidade", "Passageiros", "Viagens previstas",
         "Viagens realizadas", "No horário", "Atrasadas", "Pontualidade", "Atraso médio (min)", "Ocupação média",
         "Embarques", "Ausências", "Atrasos informados", "Incidentes"],
        lambda r: [r["name"], r["time"] or "Avulsa", r["vehicle"] or "", r["driver"] or "", _num(r["capacity"]),
                   r["passengers"], r["planned"], r["done"], r["on_time"], r["late"], _pct(r["punctuality_rate"]),
                   _num(r["avg_late_minutes"]), _pct(r["avg_occupancy"]), r["confirmed"], r["absent"],
                   r["delays"], r["incidents"]],
    ),
    "drivers": (
        ["Motorista", "Veículo", "Rotas", "Viagens previstas", "Viagens realizadas", "No horário", "Atrasadas",
         "Pontualidade", "Atraso médio (min)", "Atrasos informados", "Incidentes"],
        lambda r: [r["driver"], r["vehicle"], r["routes"], r["planned"], r["done"], r["on_time"], r["late"],
                   _pct(r["punctuality_rate"]), _num(r["avg_late_minutes"]), r["delays"], r["incidents"]],
    ),
    "events": (
        ["Data", "Hora", "Tipo", "Rota", "Turno", "Motorista", "Veículo", "Descrição"],
        lambda r: [_br(r["date"]), r["time"], "Atraso" if r["type"] == "delay" else "Incidente", r["route_name"],
                   r["shift"] or "Avulsa", r["driver"] or "", r["vehicle"] or "", r["description"]],
    ),
}


def csv_response(kind, data):
    header, row_of = CSV_LAYOUTS[kind]
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    writer.writerows(row_of(r) for r in data["rows"])
    response = HttpResponse("﻿" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="rotacerta-{kind}-{data["from"]}-a-{data["to"]}.csv"'
    return response


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class ReportView(APIView):
    permission_classes = [IsAuthenticated, IsManager]
    kind = ""
    builders = {
        "attendance": attendance_report,
        "routes": routes_report,
        "drivers": drivers_report,
        "events": events_report,
    }

    def get(self, request):
        query = ReportQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = self.builders[self.kind](request.user.person.company, query.validated_data)
        if query.validated_data.get("export") == "csv":
            return csv_response(self.kind, data)
        return Response(data)


class AttendanceDetailView(APIView):
    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request, person_id):
        query = ReportQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = attendance_detail(request.user.person.company, query.validated_data, person_id)
        if data is None:
            return Response({"detail": "Colaborador não encontrado."}, status=404)
        return Response(data)


class ReportOptionsView(APIView):
    """GET reports/options/ — turnos, rotas e motoristas da empresa, para os filtros da tela."""

    permission_classes = [IsAuthenticated, IsManager]

    def get(self, request):
        company = request.user.person.company
        schedules = Schedule.objects.filter(company=company, is_active=True).order_by("time", "id")
        routes = Route.objects.filter(company=company, is_active=True).select_related("schedule").order_by("name")
        drivers = Person.objects.filter(
            company=company, is_active=True, type=Person.PersonType.DRIVER
        ).order_by("name")
        return Response({
            "schedules": [{"id": s.id, "time": s.time.strftime("%H:%M")} for s in schedules],
            "routes": [
                {"id": r.id, "name": r.name, "schedule_id": r.schedule_id} for r in routes
            ],
            "drivers": [{"id": d.id, "name": d.name} for d in drivers],
            "max_days": MAX_DAYS,
        })
