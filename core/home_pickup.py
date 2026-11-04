"""
Ponto de embarque de um colaborador numa rota fixa, do mais ao menos comum:

1. uma parada da rota a até 500 m de casa;
2. senão, o ponto de encontro da empresa mais perto de casa, se estiver a até 500 m: ele entra na rota
   como parada nova;
3. senão, a porta de casa: a casa vira um ponto de encontro só da pessoa (MeetingPoint.home_of) e uma
   parada da rota.
Paradas novas entram na posição que menos alonga o trajeto até a empresa. Paradas de casa sem ninguém
embarcando são removidas.
"""
from django.db import transaction
from django.db.models import F

from core.geo import distance_meters
from core.models import MeetingPoint, RoutePassenger, RouteStop
from core.self_address import company_location

PICKUP_RADIUS_M = 500
# Deslocamento temporário das sequências ao reordenar (a dupla rota+sequência é única).
SEQUENCE_SHIFT = 1000


def pickup_stop(route, person, address):
    """(parada, como) onde a pessoa embarca nesta rota; como = "route" | "new_point" | "home"."""
    stops = list(route.stops.select_related("meeting_point__address"))
    public = [
        s for s in stops
        if s.meeting_point.home_of_id is None and s.meeting_point.address.latitude is not None
    ]
    if public:
        best = min(public, key=lambda s: _distance(address, s.meeting_point.address))
        if _distance(address, best.meeting_point.address) <= PICKUP_RADIUS_M:
            return best, "route"

    on_route = {s.meeting_point_id for s in stops}
    points = [
        mp for mp in MeetingPoint.objects.filter(company=route.company, is_active=True, home_of__isnull=True)
        .exclude(id__in=on_route).select_related("address")
        if mp.address.latitude is not None
    ]
    if points:
        nearest = min(points, key=lambda mp: _distance(address, mp.address))
        if _distance(address, nearest.address) <= PICKUP_RADIUS_M:
            return _insert_stop(route, nearest, stops), "new_point"

    return _insert_stop(route, _home_point(person, address), stops), "home"


def drop_unused_home_stops(route):
    """Remove paradas de casa em que ninguém mais embarca e fecha os buracos na sequência."""
    used = RoutePassenger.objects.filter(route=route, is_active=True).values("route_stop_id")
    unused = route.stops.filter(meeting_point__home_of__isnull=False).exclude(id__in=used)
    if unused.exists():
        unused.delete()
        _renumber(route, list(route.stops.order_by("sequence")))


def _insert_stop(route, point, stops):
    """Coloca o ponto na rota (ou o recoloca, se já estava) na posição mais barata."""
    others = sorted((s for s in stops if s.meeting_point_id != point.id), key=lambda s: s.sequence)
    position = _cheapest_position(others, point.address, company_location(route.company))
    with transaction.atomic():
        # A casa pode ter mudado: a parada antiga sai e entra de novo na melhor posição.
        RouteStop.objects.filter(route=route, meeting_point=point).delete()
        _renumber(route, others, insert_at=position)
        return RouteStop.objects.create(route=route, meeting_point=point, sequence=position)


def _home_point(person, address):
    name = f"Casa de {person.name} – {address.street}, {address.number}"
    point = MeetingPoint.objects.filter(home_of=person).first()
    if point is None:
        return MeetingPoint.objects.create(company=person.company, name=name, address=address, home_of=person)
    if point.address_id != address.id or point.name != name:
        point.address = address
        point.name = name
        point.save(update_fields=["address", "name", "updated_at"])
    return point


def _cheapest_position(ordered_stops, address, depot):
    """Índice em que o ponto alonga menos o trajeto (antes da parada i; len = depois da última)."""
    points = [s.meeting_point.address for s in ordered_stops]

    def leg(a, b):
        return _distance(a, b) if a is not None and b is not None else 0.0

    best_index, best_cost = len(points), float("inf")
    for i in range(len(points) + 1):
        before = points[i - 1] if i > 0 else None
        after = points[i] if i < len(points) else depot
        cost = leg(before, address) + leg(address, after) - leg(before, after)
        if cost < best_cost:
            best_index, best_cost = i, cost
    return best_index


def _renumber(route, ordered_stops, insert_at=None):
    """Reatribui 0..n às paradas na ordem dada, deixando um buraco em `insert_at` (se informado)."""
    route.stops.update(sequence=F("sequence") + SEQUENCE_SHIFT)
    for index, stop in enumerate(ordered_stops):
        stop.sequence = index + 1 if insert_at is not None and index >= insert_at else index
        RouteStop.objects.filter(pk=stop.pk).update(sequence=stop.sequence)


def _distance(a, b):
    return distance_meters(a.latitude, a.longitude, b.latitude, b.longitude)


def drop_empty_stops(route):
    """Remove da rota as paradas em que ninguém embarca (o motorista não precisa passar nelas)."""
    used = RoutePassenger.objects.filter(route=route, is_active=True).values("route_stop_id")
    empty = route.stops.exclude(id__in=used)
    removed = empty.count()
    if removed:
        empty.delete()
        _renumber(route, list(route.stops.order_by("sequence")))
    return removed


def pickup_description(ride, home=None):
    """'Flores – Rua X (a 120 m da sua casa)' ou 'a porta da sua casa', para os avisos ao passageiro."""
    point = ride.route_stop.meeting_point
    if point.home_of_id == ride.person_id:
        return "a porta da sua casa"
    home = home or ride.person.addresses.filter(is_active=True).order_by("-created_at", "-id").first()
    if home is None:
        return point.name
    return f"{point.name} (a {round(_distance(home, point.address))} m da sua casa)"
