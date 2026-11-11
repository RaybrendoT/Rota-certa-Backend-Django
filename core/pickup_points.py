"""
Pontos de embarque e demanda com H3 (grade hexagonal da Uber, https://github.com/uber/h3).

- Sugestão de pontos: colaborador a mais de `walk_limit` metros do ponto de encontro mais próximo
  está "descoberto". As casas descobertas são agrupadas por hexágono (resolução 8, ~0,74 km²);
  onde um hexágono e seus vizinhos juntam pelo menos `min_people` pessoas que caibam a pé num
  mesmo ponto, sugere-se um ponto no centro delas, com endereço de rua via Google (se houver chave).
- Mapa de demanda: quantos colaboradores moram em cada hexágono, com o contorno para desenhar
  no painel.

Só leitura: criar o ponto sugerido é decisão do gestor (cadastro de pontos de encontro).
"""
import math
from dataclasses import dataclass

import h3
from django.db.models import Prefetch

from core.geo import distance_meters
from core.geocoding import GeocodingUnavailable, reverse_geocode
from core.models import Address, MeetingPoint, Person

RESOLUTION = 8
WALK_LIMIT_METERS = 1000
MIN_PEOPLE = 5
MAX_SUGGESTIONS = 10


@dataclass
class Home:
    person: Person
    lat: float
    lng: float


def company_homes(company, person_ids=None, schedule_id=None):
    """Casas dos colaboradores ativos (opcional: só estes IDs, ou só quem anda em rotas do turno)."""
    people = Person.objects.filter(company=company, type=Person.PersonType.EMPLOYEE, is_active=True)
    if person_ids is not None:
        people = people.filter(id__in=person_ids)
    if schedule_id is not None:
        people = people.filter(
            route_passengers__route__schedule_id=schedule_id, route_passengers__is_active=True
        ).distinct()
    people = people.prefetch_related(
        Prefetch("addresses", queryset=Address.objects.filter(is_active=True).order_by("id"))
    )
    homes = []
    for person in people:
        address = next(iter(person.addresses.all()), None)
        if address is not None:
            homes.append(Home(person, float(address.latitude), float(address.longitude)))
    return homes


def meeting_point_coords(company):
    return [
        (float(mp.address.latitude), float(mp.address.longitude))
        for mp in MeetingPoint.objects.filter(company=company, is_active=True, home_of__isnull=True).select_related("address")
    ]


def _average_meters(distances):
    """Média arredondada em metros, ou None quando alguma distância é infinita (empresa sem pontos)."""
    values = list(distances)
    if not values or any(math.isinf(v) for v in values):
        return None
    return round(sum(values) / len(values))


def nearest_distance(lat, lng, points):
    return min((distance_meters(lat, lng, p_lat, p_lng) for p_lat, p_lng in points), default=float("inf"))


def suggest_pickup_points(
    company,
    person_ids=None,
    *,
    walk_limit=WALK_LIMIT_METERS,
    min_people=MIN_PEOPLE,
    resolution=RESOLUTION,
    max_suggestions=MAX_SUGGESTIONS,
    with_address=True,
):
    homes = company_homes(company, person_ids)
    points = meeting_point_coords(company)
    distance_now = {id(home): nearest_distance(home.lat, home.lng, points) for home in homes}
    uncovered = [home for home in homes if distance_now[id(home)] > walk_limit]

    by_cell = {}
    for home in uncovered:
        by_cell.setdefault(h3.latlng_to_cell(home.lat, home.lng, resolution), []).append(home)

    remaining = {id(home): home for home in uncovered}
    exhausted = set()
    suggestions = []
    while len(suggestions) < max_suggestions:
        candidates = [cell for cell in by_cell if cell not in exhausted and any(id(h) in remaining for h in by_cell[cell])]
        if not candidates:
            break

        def neighborhood(cell):
            return [h for c in h3.grid_disk(cell, 1) for h in by_cell.get(c, []) if id(h) in remaining]

        cell = max(candidates, key=lambda c: len(neighborhood(c)))
        group = neighborhood(cell)
        if len(group) < min_people:
            break
        lat = sum(h.lat for h in group) / len(group)
        lng = sum(h.lng for h in group) / len(group)
        # Só entra quem chega a pé no ponto novo.
        members = [h for h in group if distance_meters(h.lat, h.lng, lat, lng) <= walk_limit]
        if len(members) < min_people:
            exhausted.add(cell)
            continue
        lat = sum(h.lat for h in members) / len(members)
        lng = sum(h.lng for h in members) / len(members)
        for home in members:
            remaining.pop(id(home))
        after = [distance_meters(h.lat, h.lng, lat, lng) for h in members]
        suggestions.append({
            "h3_cell": h3.latlng_to_cell(lat, lng, resolution),
            "latitude": round(lat, 6),
            "longitude": round(lng, 6),
            "people": len(members),
            "person_ids": [h.person.id for h in members],
            "names": [h.person.name for h in members],
            # Sem nenhum ponto cadastrado a distância atual é infinita: vai como None (round(inf) quebrava).
            "avg_distance_now_m": _average_meters(distance_now[id(h)] for h in members),
            "avg_distance_after_m": round(sum(after) / len(after)),
            "address": _address_of(lat, lng) if with_address else None,
        })

    return {
        "walk_limit_m": walk_limit,
        "min_people": min_people,
        "resolution": resolution,
        "employees": len(homes),
        "covered": len(homes) - len(uncovered),
        "uncovered": len(uncovered),
        "still_uncovered": len(remaining),
        "suggestions": suggestions,
    }


def demand_map(company, schedule_id=None, resolution=RESOLUTION):
    """Hexágonos com quantidade de colaboradores, do mais cheio para o mais vazio."""
    counts = {}
    for home in company_homes(company, schedule_id=schedule_id):
        cell = h3.latlng_to_cell(home.lat, home.lng, resolution)
        counts[cell] = counts.get(cell, 0) + 1
    return [
        {
            "h3_cell": cell,
            "count": count,
            "center": list(h3.cell_to_latlng(cell)),
            "boundary": [list(vertex) for vertex in h3.cell_to_boundary(cell)],
        }
        for cell, count in sorted(counts.items(), key=lambda item: -item[1])
    ]


def _address_of(lat, lng):
    """Endereço da sugestão; sem chave ou com o Google fora do ar, a sugestão sai só com a coordenada."""
    try:
        return reverse_geocode(lat, lng)
    except GeocodingUnavailable:
        return None
