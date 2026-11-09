"""
Criação e edição manual de rotas pelo painel (stepper "Nova rota" / "Editar rota").

É o mesmo resultado do planejamento com IA (core.planning.planner), mas o gestor escolhe tudo:
turno (e data, na rota avulsa), ônibus, colaboradores e paradas. O servidor completa e confere:

- quem ainda não tem parada embarca no ponto de encontro mais perto de casa;
- a ordem das paradas é a de menor trajeto até a empresa quando o gestor pede (`optimize`) ou
  quando ainda não há paradas; senão, vale a ordem que ele montou;
- horários estimados, lotação, conflito do motorista e quem embarca longe de casa.

A entrada é a mesma na prévia e na gravação:
    {"name": "...", "kind": "fixed" | "extra", "schedule": 3, "service_date": "2026-11-13" | null,
     "vehicle": 7, "passengers": [11, 12, 13],
     "stops": [{"meeting_point": 5, "passengers": [11, 12]}] | null, "optimize": false}
"""
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timedelta
from types import SimpleNamespace

from django.db import transaction
from django.db.models import F

from core import route_optimizer
from core.dates import local_today
from core.geo import distance_meters
from core.home_pickup import SEQUENCE_SHIFT
from core.models import (
    MeetingPoint,
    Notification,
    Person,
    Route,
    RoutePassenger,
    RouteStop,
    Schedule,
    Vehicle,
)
from core.notifications import notify
from core.planning.planner import (
    DEPARTURE_MARGIN_MINUTES,
    MAX_STOPS,
    ROUTE_WINDOW_MINUTES,
    WEEKDAY_CODES,
    active_employees,
    short_point_name,
)
from core.self_address import company_location

# Ponto de embarque mais longe que isso da casa vira aviso (a regra das rotas fixas é 500 m).
FAR_PICKUP_METERS = 1000


class BuilderError(Exception):
    """Entrada que não dá para montar (turno, ônibus ou pessoas de fora da empresa)."""


class BuilderInvalid(Exception):
    """A rota tem erros que impedem salvar; `result` é a prévia com os avisos."""

    def __init__(self, result):
        super().__init__("A rota tem erros que impedem salvar.")
        self.result = result


@dataclass
class Draft:
    name: str
    kind: str
    schedule: Schedule
    service_date: date_cls | None
    vehicle: Vehicle | None
    people: dict  # id → Person
    stops: list = field(default_factory=list)  # [(ponto, [ids])], na ordem da rota
    without_home: list = field(default_factory=list)


# ---------------------------------------------------------------------- consultas do stepper

def options(company):
    """Turnos, colaboradores (com o ponto mais perto de casa) e pontos de encontro da empresa."""
    points = _public_points(company)
    rides = {}
    for person_id, route_name, route_time in RoutePassenger.objects.filter(
        route__company=company, is_active=True, route__is_active=True, route__service_date__isnull=True
    ).values_list("person_id", "route__name", "route__schedule__time"):
        rides.setdefault(person_id, []).append(
            f"{route_time:%H:%M} {route_name}" if route_time else route_name
        )

    employees = []
    for person in active_employees(company).order_by("name"):
        home = _home(person)
        nearest = _nearest(home, points) if home is not None and points else None
        employees.append({
            "id": person.id,
            "name": person.name,
            "code": person.employee_code,
            "bairro": (home.neighborhood or "") if home else "",
            "has_home": home is not None,
            "nearest_point": (
                {"id": nearest[0].id, "name": nearest[0].name, "distance_m": round(nearest[1])} if nearest else None
            ),
            "fixed_routes": rides.get(person.id, []),
        })

    depot = company_location(company)
    return {
        "schedules": [
            {"id": s.id, "time": f"{s.time:%H:%M}", "weekdays": s.weekdays, "label": str(s)}
            for s in Schedule.objects.filter(company=company, is_active=True).order_by("time")
        ],
        "employees": employees,
        "meeting_points": [_point_payload(p) for p in points],
        "company_location": (
            {"lat": float(depot.latitude), "lng": float(depot.longitude)}
            if depot is not None and depot.latitude is not None else None
        ),
    }


def vehicles(company, *, schedule_id, service_date=None, route=None):
    """Ônibus da empresa com o motorista e o conflito (outra rota perto do horário), se houver."""
    schedule = _schedule(company, schedule_id)
    items = []
    for vehicle in Vehicle.objects.filter(company=company, is_active=True).select_related("driver").order_by("plate_number"):
        driver = vehicle.driver if vehicle.driver_id and vehicle.driver.is_active else None
        items.append({
            "id": vehicle.id,
            "plate": vehicle.plate_number,
            "model": vehicle.model,
            "capacity": vehicle.capacity,
            "driver": {"id": driver.id, "name": driver.name} if driver else None,
            "conflict": _driver_conflict(driver, schedule, service_date, route) if driver else None,
        })
    return {"schedule": schedule.id, "service_date": service_date.isoformat() if service_date else None, "vehicles": items}


def preview(company, data, route=None):
    return _describe(company, _resolve(company, data, route), route)


def load(route):
    """A rota no formato de entrada do stepper, com a prévia calculada."""
    rides = list(
        RoutePassenger.objects.filter(route=route, is_active=True, person__is_active=True)
        .select_related("route_stop")
        .order_by("route_stop__sequence", "person__name")
    )
    stops = []
    for stop in route.stops.order_by("sequence"):
        stops.append({"meeting_point": stop.meeting_point_id, "passengers": [r.person_id for r in rides if r.route_stop_id == stop.id]})
    data = {
        "name": route.name,
        "kind": "extra" if route.service_date else "fixed",
        "schedule": route.schedule_id,
        "service_date": route.service_date,
        "vehicle": route.vehicle_id,
        "passengers": [r.person_id for r in rides],
        "stops": stops,
        "optimize": False,
    }
    result = preview(route.company, data, route) if route.schedule_id else None
    return {
        "id": route.id,
        "data": {**data, "service_date": route.service_date.isoformat() if route.service_date else None},
        "preview": result,
    }


@transaction.atomic
def save(company, data, route=None):
    """Cria (route=None) ou atualiza a rota com paradas e passageiros e avisa quem mudou."""
    draft = _resolve(company, data, route)
    result = _describe(company, draft, route)
    if not draft.name.strip():
        result["warnings"].insert(0, _warning("error", "name", "Dê um nome para a rota."))
        result["can_save"] = False
    if not result["can_save"]:
        raise BuilderInvalid(result)

    is_new = route is None
    previous_driver = route.vehicle.driver_id if route is not None and route.vehicle_id else None
    fields = {
        "name": draft.name.strip(),
        "vehicle": draft.vehicle,
        "schedule": draft.schedule,
        "service_date": draft.service_date if draft.kind == "extra" else None,
    }
    if is_new:
        route = Route.objects.create(company=company, **fields)
    else:
        route = Route.objects.select_for_update().get(pk=route.pk)
        for name, value in fields.items():
            setattr(route, name, value)
        route.save(update_fields=[*fields, "updated_at"])

    # Paradas: reaproveita as que continuam (o histórico de chegada fica), apaga as que saíram.
    existing = {stop.meeting_point_id: stop for stop in route.stops.all()}
    route.stops.update(sequence=F("sequence") + SEQUENCE_SHIFT)
    placed = []
    for sequence, (point, ids) in enumerate(draft.stops):
        stop = existing.get(point.id)
        if stop is None:
            stop = RouteStop.objects.create(route=route, meeting_point_id=point.id, sequence=sequence)
        else:
            RouteStop.objects.filter(pk=stop.pk).update(sequence=sequence)
        placed.append((stop, point, ids))
    route.stops.exclude(pk__in=[stop.pk for stop, _, _ in placed]).delete()

    # Passageiros: quem sai fica inativo (guarda o histórico de embarque); quem volta é reativado.
    rides = {ride.person_id: ride for ride in RoutePassenger.objects.filter(route=route)}
    before = {pid: ride.route_stop_id for pid, ride in rides.items() if ride.is_active}
    stop_of = {}
    for stop, _, ids in placed:
        for person_id in ids:
            stop_of[person_id] = stop
            ride = rides.get(person_id)
            if ride is None:
                RoutePassenger.objects.create(person_id=person_id, route=route, route_stop=stop)
            elif not ride.is_active or ride.route_stop_id != stop.pk:
                RoutePassenger.objects.filter(pk=ride.pk).update(route_stop=stop, is_active=True)
    removed = [pid for pid in before if pid not in stop_of]
    RoutePassenger.objects.filter(route=route, person_id__in=removed).update(is_active=False)

    _notify_changes(route, draft, result, before, stop_of, removed, is_new, previous_driver)
    return route, _describe(company, _resolve(company, {**data, "stops": _stops_input(placed)}, route), route)


# ---------------------------------------------------------------------- montagem

def _resolve(company, data, route):
    kind = data.get("kind") or "fixed"
    if kind not in ("fixed", "extra"):
        raise BuilderError("Tipo de rota inválido: use fixed ou extra.")
    schedule = _schedule(company, data.get("schedule"))
    service_date = _parse_date(data.get("service_date")) if kind == "extra" else None

    vehicle = None
    if data.get("vehicle"):
        vehicle = Vehicle.objects.filter(company=company, is_active=True, pk=data["vehicle"]).select_related("driver").first()
        if vehicle is None:
            raise BuilderError("Ônibus não encontrado nesta empresa.")

    ids = list(dict.fromkeys(int(pid) for pid in data.get("passengers") or []))
    people = {p.id: p for p in active_employees(company).filter(id__in=ids)}
    missing = set(ids) - set(people)
    if missing:
        raise BuilderError(f"Colaboradores que não são ativos nesta empresa: {sorted(missing)}.")

    public = _public_points(company)
    points = {p.id: p for p in public}
    wanted_points = {int(s["meeting_point"]) for s in data.get("stops") or []} - set(points)
    if wanted_points:
        # Paradas de casa (embarque na porta) só valem nesta empresa; em geral vêm de uma rota existente.
        for mp in MeetingPoint.objects.filter(company=company, is_active=True, id__in=wanted_points).select_related("address"):
            if mp.address.latitude is not None:
                points[mp.id] = _point(mp)
        unknown = wanted_points - set(points)
        if unknown:
            raise BuilderError(f"Pontos de encontro que não existem nesta empresa: {sorted(unknown)}.")

    draft = Draft(
        name=data.get("name") or "", kind=kind, schedule=schedule, service_date=service_date,
        vehicle=vehicle, people=people,
    )
    seen = set()
    for item in data.get("stops") or []:
        point = points[int(item["meeting_point"])]
        if any(p.id == point.id for p, _ in draft.stops):
            continue
        riders = []
        for pid in item.get("passengers") or []:
            pid = int(pid)
            if pid in people and pid not in seen:
                riders.append(pid)
                seen.add(pid)
        draft.stops.append((point, riders))

    depot = _depot(company)
    for pid in ids:
        if pid in seen:
            continue
        home = _home(people[pid])
        if home is None:
            draft.without_home.append(people[pid])
            continue
        if not public:
            raise BuilderError("A empresa não tem pontos de encontro cadastrados para usar como paradas.")
        point, _ = _nearest(home, public)
        stop = next((s for s in draft.stops if s[0].id == point.id), None)
        if stop is None:
            stop = (point, [])
            draft.stops.insert(_cheapest_position(draft.stops, point, depot), stop)
        stop[1].append(pid)

    if draft.stops and (data.get("optimize") or data.get("stops") is None):
        draft.stops = route_optimizer.order_route(draft.stops, depot)
    return draft


def _describe(company, draft, route):
    warnings = []
    vehicle = draft.vehicle
    driver = vehicle.driver if vehicle is not None and vehicle.driver_id and vehicle.driver.is_active else None
    load = sum(len(ids) for _, ids in draft.stops)

    if vehicle is None:
        warnings.append(_warning("error", "vehicle", "Escolha o ônibus da rota."))
    elif driver is None:
        warnings.append(_warning("error", "driver", f"O ônibus {vehicle.plate_number} não tem motorista: cadastre em Veículos."))
    else:
        conflict = _driver_conflict(driver, draft.schedule, draft.service_date, route)
        if conflict:
            warnings.append(_warning("error", "driver_busy", f"{driver.name} já tem a rota {conflict} perto deste horário."))
    if vehicle is not None and load > vehicle.capacity:
        warnings.append(_warning("error", "capacity", f"{load} pessoas para {vehicle.capacity} lugares no {vehicle.plate_number}."))
    if not draft.people:
        warnings.append(_warning("error", "passengers", "Escolha os colaboradores da rota."))
    if draft.without_home:
        warnings.append(_warning(
            "error", "without_home", "Sem endereço cadastrado (não dá para escolher o ponto): "
            + ", ".join(p.name for p in draft.without_home) + ".", [p.id for p in draft.without_home],
        ))
    if draft.kind == "extra":
        if draft.service_date is None:
            warnings.append(_warning("error", "service_date", "Informe a data da rota avulsa."))
        elif draft.service_date < local_today():
            warnings.append(_warning("error", "service_date", f"A data {draft.service_date:%d/%m/%Y} já passou."))

    day = draft.service_date or local_today()
    arrival = datetime.combine(day, draft.schedule.time)
    depot = _depot(company)
    stops, far = [], []
    minutes_before, meters = route_optimizer.route_timeline(draft.stops, depot) if draft.stops else ([], 0)
    for (point, ids), minutes in zip(draft.stops, minutes_before):
        passengers = []
        for pid in ids:
            person = draft.people[pid]
            home = _home(person)
            distance = round(distance_meters(float(home.latitude), float(home.longitude), point.lat, point.lng)) if home else None
            if distance is not None and distance > FAR_PICKUP_METERS and point.home_of_id is None:
                far.append(f"{person.name} ({distance / 1000:.1f} km)".replace(".", ","))
            passengers.append({"id": pid, "name": person.name, "code": person.employee_code, "distance_m": distance})
        stops.append({
            "meeting_point": _point_payload(point),
            "eta": (arrival - timedelta(minutes=minutes)).strftime("%H:%M"),
            "passengers": passengers,
        })
        if not ids:
            warnings.append(_warning("warning", "empty_stop", f"Ninguém embarca em {point.name}: o ônibus passa lá à toa."))
    if far:
        warnings.append(_warning("warning", "far_pickup", f"{len(far)} embarcam a mais de 1 km de casa: " + "; ".join(far) + "."))
    if len(draft.stops) > MAX_STOPS:
        warnings.append(_warning("warning", "stops", f"{len(draft.stops)} paradas: acima de {MAX_STOPS} a viagem fica longa."))
    warnings.extend(_double_booked(draft, route))

    duration = (minutes_before[0] if minutes_before else 0) + (DEPARTURE_MARGIN_MINUTES if minutes_before else 0)
    errors = [w for w in warnings if w["level"] == "error"]
    return {
        "name": draft.name,
        "kind": draft.kind,
        "schedule": {"id": draft.schedule.id, "time": f"{draft.schedule.time:%H:%M}", "label": str(draft.schedule)},
        "service_date": draft.service_date.isoformat() if draft.service_date else None,
        "vehicle": (
            {"id": vehicle.id, "plate": vehicle.plate_number, "model": vehicle.model, "capacity": vehicle.capacity}
            if vehicle else None
        ),
        "driver": {"id": driver.id, "name": driver.name} if driver else None,
        "load": load,
        "arrival": f"{draft.schedule.time:%H:%M}",
        "departure": (arrival - timedelta(minutes=duration)).strftime("%H:%M") if minutes_before else None,
        "duration_min": duration,
        "distance_km": round(meters / 1000, 1),
        "stops": stops,
        "warnings": sorted(warnings, key=lambda w: 0 if w["level"] == "error" else 1),
        "can_save": not errors,
        "times_are_estimates": True,
    }


def _double_booked(draft, route):
    """Colaboradores que já vão em outra rota que se sobrepõe a esta (mesmos dias, horário próximo)."""
    found = {}
    rides = (
        RoutePassenger.objects.filter(
            person_id__in=list(draft.people), is_active=True, route__is_active=True, route__schedule__isnull=False
        )
        .exclude(route_id=getattr(route, "pk", None))
        .select_related("route__schedule", "person")
    )
    for ride in rides:
        if _overlaps(ride.route, draft.schedule, draft.service_date) and _near_in_time(ride.route.schedule, draft.schedule):
            found.setdefault(ride.route, []).append(ride.person.name)
    return [
        _warning(
            "warning", "double_booked",
            f"Já vão na rota {other.schedule.time:%H:%M} {other.name}: " + ", ".join(sorted(names)) + ".",
        )
        for other, names in found.items()
    ]


def _driver_conflict(driver, schedule, service_date, route):
    others = (
        Route.objects.filter(vehicle__driver=driver, is_active=True, schedule__isnull=False)
        .exclude(pk=getattr(route, "pk", None))
        .select_related("schedule")
    )
    for other in others:
        if _overlaps(other, schedule, service_date) and _near_in_time(other.schedule, schedule):
            return f"{other.schedule.time:%H:%M} {other.name}"
    return None


def _overlaps(other, schedule, service_date):
    """A outra rota roda em algum dia desta? (fixa: dias do turno; avulsa: a data)."""
    other_days = set(other.schedule.weekdays or [])
    if service_date is not None:
        if other.service_date is not None:
            return other.service_date == service_date
        return WEEKDAY_CODES[service_date.weekday()] in other_days
    days = set(schedule.weekdays or [])
    if other.service_date is not None:
        return other.service_date >= local_today() and WEEKDAY_CODES[other.service_date.weekday()] in days
    return bool(days & other_days)


def _near_in_time(a, b):
    return abs((a.time.hour * 60 + a.time.minute) - (b.time.hour * 60 + b.time.minute)) < ROUTE_WINDOW_MINUTES


def _notify_changes(route, draft, result, before, stop_of, removed, is_new, previous_driver):
    when = f"em {draft.service_date:%d/%m}" if draft.service_date else f"({draft.schedule.weekdays_label()})"
    eta_by_point = {s["meeting_point"]["id"]: s["eta"] for s in result["stops"]}
    names = {p.id: p.name for p, _ in draft.stops}

    def where(person_id):
        point_id = stop_of[person_id].meeting_point_id
        return f"às {eta_by_point.get(point_id, '')} em {names.get(point_id, '')}"

    added = [pid for pid in stop_of if pid not in before]
    moved = [pid for pid in stop_of if pid in before and before[pid] != stop_of[pid].pk]
    arrival = f"{draft.schedule.time:%H:%M}"
    for person_id in added:
        notify(
            [draft.people[person_id]], kind=Notification.Kind.ROUTE_ASSIGNED, title=f"Nova rota {when}",
            message=f"{route.name}: embarque {where(person_id)}. Chegada às {arrival}.", route=route,
        )
    for person_id in moved:
        notify(
            [draft.people[person_id]], kind=Notification.Kind.PICKUP_CHANGED, title="Ponto de embarque alterado",
            message=f"{route.name}: agora o embarque é {where(person_id)}.", route=route,
        )
    if removed:
        notify(
            Person.objects.filter(id__in=removed), kind=Notification.Kind.ROUTE_UPDATED, title="Rota atualizada",
            message=f"Você não está mais na rota {route.name}. Fale com o gestor de transporte se precisar.", route=route,
        )
    driver = draft.vehicle.driver if draft.vehicle else None
    if driver is not None and (is_new or driver.id != previous_driver):
        notify(
            [driver], kind=Notification.Kind.ROUTE_ASSIGNED, title=f"Nova rota {when}",
            message=f"{route.name}: saída às {result['departure']}, {len(result['stops'])} paradas, {result['load']} passageiros.",
            route=route,
        )


# ---------------------------------------------------------------------- apoio

def _schedule(company, schedule_id):
    schedule = Schedule.objects.filter(company=company, is_active=True, pk=schedule_id).first() if schedule_id else None
    if schedule is None:
        raise BuilderError("Escolha um turno (horário de chegada) desta empresa.")
    return schedule


def _parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date_cls):
        return value
    try:
        return date_cls.fromisoformat(str(value))
    except ValueError:
        raise BuilderError("Data inválida; use AAAA-MM-DD.") from None


def _public_points(company):
    return [
        _point(mp)
        for mp in MeetingPoint.objects.filter(company=company, is_active=True, home_of__isnull=True).select_related("address").order_by("name")
        if mp.address.latitude is not None
    ]


def _point(mp):
    return SimpleNamespace(
        id=mp.id, name=mp.name, lat=float(mp.address.latitude), lng=float(mp.address.longitude), home_of_id=mp.home_of_id
    )


def _point_payload(point):
    return {
        "id": point.id, "name": point.name, "short_name": short_point_name(point.name),
        "lat": point.lat, "lng": point.lng, "home": point.home_of_id is not None,
    }


def _home(person):
    addresses = [a for a in person.addresses.all() if a.latitude is not None]
    return addresses[0] if addresses else None


def _nearest(home, points):
    lat, lng = float(home.latitude), float(home.longitude)
    point = min(points, key=lambda p: distance_meters(lat, lng, p.lat, p.lng))
    return point, distance_meters(lat, lng, point.lat, point.lng)


def _depot(company):
    address = company_location(company)
    if address is None or address.latitude is None:
        raise BuilderError("A empresa não tem endereço cadastrado (destino das rotas).")
    return SimpleNamespace(lat=float(address.latitude), lng=float(address.longitude))


def _cheapest_position(stops, point, depot):
    """Índice em que o ponto novo alonga menos o trajeto até a empresa."""
    def dist(a, b):
        return distance_meters(a.lat, a.lng, b.lat, b.lng)

    best_index, best_cost = len(stops), float("inf")
    for index in range(len(stops) + 1):
        before = stops[index - 1][0] if index > 0 else None
        after = stops[index][0] if index < len(stops) else depot
        cost = (dist(before, point) if before else 0) + dist(point, after) - (dist(before, after) if before else 0)
        if cost < best_cost:
            best_index, best_cost = index, cost
    return best_index


def _stops_input(placed):
    return [{"meeting_point": point.id, "passengers": list(ids)} for _, point, ids in placed]


def _warning(level, code, message, person_ids=None):
    warning = {"level": level, "code": code, "message": message}
    if person_ids:
        warning["person_ids"] = person_ids
    return warning
