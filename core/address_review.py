"""
Aprovação, pelo gestor, dos endereços enviados pelos colaboradores no app (core.self_address).

Aprovar troca o endereço oficial da pessoa (o anterior vira histórico) e, opcionalmente, já a coloca
numa rota fixa e atualiza o ponto de embarque das rotas em que ela está. Recusar mantém o endereço
anterior. Nos dois casos o colaborador é avisado no app.
"""
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.utils import timezone

from core.geo import distance_meters
from core.home_pickup import PICKUP_RADIUS_M, drop_unused_home_stops, pickup_stop
from core.models import Address, MeetingPoint, Notification, Person, Route, RoutePassenger, RouteStop
from core.notifications import notify
from core.self_address import address_payload, nearest_point

# Rotas fixas com uma parada até essa distância da casa entram nas sugestões.
SUGGESTION_RADIUS_M = 2_000
MAX_SUGGESTIONS = 6
FALLBACK_SUGGESTIONS = 3
WEEKDAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


class ReviewError(Exception):
    """Regra de negócio na aprovação; a mensagem vai para o gestor."""


def review_queryset(company):
    return (
        Address.objects.filter(person__company=company, source__in=(Address.Source.GPS, Address.Source.TYPED))
        .exclude(review_status="")
        .select_related("person", "reviewed_by")
        .order_by("-created_at", "-id")
    )


def request_payload(address: Address) -> dict:
    """Um pedido com tudo que o gestor precisa para decidir: antes/depois, distância, rotas e sugestões."""
    person = address.person
    previous = (
        person.addresses.filter(is_active=True).exclude(pk=address.pk).order_by("-created_at", "-id").first()
        if address.review_status == Address.Review.PENDING
        else None
    )
    return {
        "id": address.id,
        "status": address.review_status,
        "person": {"id": person.id, "name": person.name, "code": person.employee_code or person.license_number, "type": person.type},
        "address": address_payload(address),
        "nearest_point": nearest_point(person.company, address.latitude, address.longitude),
        "previous_address": address_payload(previous) if previous else None,
        "moved_m": round(distance_meters(previous.latitude, previous.longitude, address.latitude, address.longitude)) if previous else None,
        "current_routes": current_routes(person),
        "suggestions": route_suggestions(person, address.latitude, address.longitude)
        if address.review_status == Address.Review.PENDING
        else [],
        "reviewed_by": address.reviewed_by.name if address.reviewed_by else None,
    }


def current_routes(person: Person) -> list:
    rides = (
        RoutePassenger.objects.filter(person=person, is_active=True, route__is_active=True, route__service_date__isnull=True)
        .select_related("route__schedule", "route_stop__meeting_point")
        .order_by("route__schedule__time")
    )
    return [
        {
            "route_id": ride.route_id,
            "name": ride.route.name,
            "arrival": _arrival(ride.route),
            "stop": ride.route_stop.meeting_point.name if ride.route_stop else None,
        }
        for ride in rides
    ]


def route_suggestions(person: Person, lat, lng) -> list:
    """Rotas fixas que passam perto da casa, das mais próximas para as mais distantes, com lotação."""
    current = set(
        RoutePassenger.objects.filter(person=person, is_active=True).values_list("route_id", flat=True)
    )
    routes = (
        Route.objects.filter(company=person.company, is_active=True, service_date__isnull=True)
        .select_related("schedule", "vehicle")
        .annotate(load=Count("route_passengers", filter=Q(route_passengers__is_active=True), distinct=True))
        .prefetch_related(Prefetch("stops", queryset=RouteStop.objects.select_related("meeting_point__address")))
    )
    # Ponto de encontro da empresa mais perto da casa: vira parada nova se a rota não tiver uma perto.
    company_points = [
        mp for mp in MeetingPoint.objects.filter(company=person.company, is_active=True, home_of__isnull=True).select_related("address")
        if mp.address.latitude is not None
    ]
    nearest_company_point = min(
        company_points, key=lambda mp: distance_meters(lat, lng, mp.address.latitude, mp.address.longitude), default=None
    )
    near_point_ok = nearest_company_point is not None and distance_meters(
        lat, lng, nearest_company_point.address.latitude, nearest_company_point.address.longitude
    ) <= PICKUP_RADIUS_M
    candidates = []
    for route in routes:
        # Paradas na casa de outros colaboradores não servem de ponto de encontro.
        stops = [
            s for s in route.stops.all()
            if s.meeting_point.address.latitude is not None and s.meeting_point.home_of_id in (None, person.id)
        ]
        if not stops:
            continue
        best = min(stops, key=lambda s: distance_meters(lat, lng, s.meeting_point.address.latitude, s.meeting_point.address.longitude))
        meters = distance_meters(lat, lng, best.meeting_point.address.latitude, best.meeting_point.address.longitude)
        on_route = route.id in current
        capacity = route.vehicle.capacity if route.vehicle else 0
        candidates.append({
            "route_id": route.id,
            "name": route.name,
            "arrival": _arrival(route),
            "weekdays": _weekdays(route),
            "stop_id": best.id,
            "stop": best.meeting_point.name,
            "distance_m": round(meters),
            "load": route.load,
            "capacity": capacity,
            "free_seats": max(capacity - route.load, 0),
            "already_on_route": on_route,
            "far": meters > SUGGESTION_RADIUS_M,
            # Como embarcaria se incluído nesta rota (mesma regra de core.home_pickup).
            "pickup": "route" if meters <= PICKUP_RADIUS_M else "new_point" if near_point_ok else "home",
            "pickup_point": nearest_company_point.name if near_point_ok and meters > PICKUP_RADIUS_M else None,
        })
    suggestions = [c for c in candidates if not c["far"] or c["already_on_route"]]
    if not suggestions:
        # Nenhuma rota passa perto: mostra as mais próximas mesmo assim (marcadas como longe) para o gestor decidir.
        suggestions = sorted(candidates, key=lambda c: c["distance_m"])[:FALLBACK_SUGGESTIONS]
    # Lotadas por último (só servem para quem já está nela); depois pela distância até a parada.
    suggestions.sort(key=lambda s: (s["free_seats"] == 0 and not s["already_on_route"], s["distance_m"]))
    return suggestions[:MAX_SUGGESTIONS]


def approve(address: Address, manager: Person, *, route_id=None, update_current_stops=True, note="") -> dict:
    if address.review_status != Address.Review.PENDING:
        raise ReviewError("Este endereço já foi revisado.")
    person = address.person
    changes = []
    with transaction.atomic():
        person.addresses.filter(is_active=True).exclude(pk=address.pk).update(is_active=False, updated_at=timezone.now())
        address.is_active = True
        address.review_status = Address.Review.APPROVED
        address.reviewed_by = manager
        address.reviewed_at = timezone.now()
        address.review_note = note.strip()
        address.save(update_fields=["is_active", "review_status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])

        if update_current_stops:
            for ride in RoutePassenger.objects.filter(
                person=person, is_active=True, route__is_active=True, route__service_date__isnull=True
            ).exclude(route_id=route_id).select_related("route"):
                previous_stop = ride.route_stop_id
                stop, how = pickup_stop(ride.route, person, address)
                ride.route_stop = stop
                ride.save(update_fields=["route_stop", "updated_at"])
                drop_unused_home_stops(ride.route)
                if stop.id != previous_stop:
                    changes.append(f"Na rota {ride.route.name}, o embarque passou para {_where(stop, how)}.")
        if route_id:
            changes.append(_assign_route(person, address, route_id))

    message = "Seu novo endereço foi aprovado pelo gestor de transporte."
    if changes:
        message += " " + " ".join(changes)
    notify([person], kind=Notification.Kind.ADDRESS_APPROVED, title="Endereço aprovado", message=message, data={"address_id": address.id})
    return {**request_payload(address), "changes": changes}


def reject(address: Address, manager: Person, reason: str) -> dict:
    if address.review_status != Address.Review.PENDING:
        raise ReviewError("Este endereço já foi revisado.")
    reason = reason.strip()
    if not reason:
        raise ReviewError("Informe o motivo da recusa; ele aparece para o colaborador no app.")
    address.review_status = Address.Review.REJECTED
    address.reviewed_by = manager
    address.reviewed_at = timezone.now()
    address.review_note = reason[:255]
    address.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
    notify(
        [address.person],
        kind=Notification.Kind.ADDRESS_REJECTED,
        title="Endereço recusado",
        message=f"O gestor de transporte recusou o novo endereço: {address.review_note} Seu endereço anterior continua valendo.",
        data={"address_id": address.id},
    )
    return request_payload(address)


def _assign_route(person, address, route_id) -> str:
    route = (
        Route.objects.select_for_update(of=("self",))
        .filter(pk=route_id, company=person.company, is_active=True, service_date__isnull=True)
        .select_related("vehicle")
        .first()
    )
    if route is None:
        raise ReviewError("Rota fixa não encontrada nesta empresa.")
    ride = RoutePassenger.objects.filter(person=person, route=route).first()
    if ride is None or not ride.is_active:
        load = RoutePassenger.objects.filter(route=route, is_active=True).count()
        if route.vehicle and load >= route.vehicle.capacity:
            raise ReviewError(f"A rota {route.name} está lotada ({load}/{route.vehicle.capacity}).")
    stop, how = pickup_stop(route, person, address)
    if ride is None:
        RoutePassenger.objects.create(person=person, route=route, route_stop=stop)
    else:
        ride.route_stop = stop
        ride.is_active = True
        ride.save(update_fields=["route_stop", "is_active", "updated_at"])
    drop_unused_home_stops(route)
    return f"Você foi incluído(a) na rota {route.name}, com embarque {_where(stop, how, preposition=True)}."


def _where(stop, how, preposition=False):
    """Como o embarque aparece nas mensagens: o ponto de encontro ou a porta de casa."""
    if how == "home":
        where = "na porta da sua casa" if preposition else "a porta da sua casa"
        return f"{where} (nenhum ponto de encontro a menos de {PICKUP_RADIUS_M} m)"
    return f"em {stop.meeting_point.name}" if preposition else stop.meeting_point.name


def _arrival(route):
    return route.schedule.time.strftime("%H:%M") if route.schedule else None


def _weekdays(route):
    if not route.schedule:
        return []
    return sorted(route.schedule.weekdays, key=lambda d: WEEKDAY_ORDER.index(d) if d in WEEKDAY_ORDER else 9)
