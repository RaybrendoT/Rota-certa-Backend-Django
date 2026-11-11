"""
Monta as rotas fixas de um turno passando por todos os colaboradores da empresa.

Mesma lógica do planejamento com IA (core.planning.planner), mas para rotas fixas e com a frota
inteira, de capacidades diferentes:
1. cada colaborador embarca no ponto de encontro mais perto de casa;
2. os pontos são varridos pelo ângulo em relação à empresa e agrupados em fatias vizinhas, cada
   fatia até a lotação do seu ônibus (com folga para quem entrar depois) e o limite de paradas.
   Ponto com mais gente do que cabe é dividido entre duas rotas vizinhas;
3. usa o menor número de ônibus em que a viagem mais longa cabe na meta (90 min); se nenhum
   couber, o de viagem mais curta;
4. dentro da rota, a ordem das paradas é a de menor trajeto até a empresa.

Ficam de fora: quem não tem endereço, quem já está numa rota fixa que se sobrepõe ao turno e,
por padrão, gestores e gerentes. Motoristas com outra rota perto do horário não entram.

Uso pelo comando `python manage.py criar_rotas_fixas` (ver --help).
"""
import math
from dataclasses import dataclass, field

from django.db import transaction

from core import route_optimizer
from core.models import AccessAccount, Notification, Route, RoutePassenger, RouteStop, Vehicle
from core.notifications import notify
from core.planning.planner import DEPARTURE_MARGIN_MINUTES, active_employees, short_point_name
from core.route_builder import (
    FAR_PICKUP_METERS,
    _depot,
    _driver_conflict,
    _home,
    _near_in_time,
    _nearest,
    _overlaps,
    _public_points,
)

TARGET_RIDE_MINUTES = 90
MAX_STOPS = 12
SLACK_PERCENT = 10


class FixedRoutesError(Exception):
    """Não dá para montar (sem pontos, sem ônibus livres, não cabe todo mundo...)."""


@dataclass
class PlannedRoute:
    number: int
    vehicle: Vehicle
    stops: list  # [(ponto, [Person])], na ordem da viagem
    minutes_before: list
    meters: float

    @property
    def load(self):
        return sum(len(riders) for _, riders in self.stops)

    @property
    def ride_minutes(self):
        return (self.minutes_before[0] if self.minutes_before else 0) + DEPARTURE_MARGIN_MINUTES

    @property
    def name_suffix(self):
        by_load = sorted(self.stops, key=lambda item: -len(item[1]))
        names = list(dict.fromkeys(short_point_name(point.name) for point, _ in by_load))
        return " · ".join(names[:2])


@dataclass
class Plan:
    company: object
    schedule: object
    routes: list = field(default_factory=list)
    without_home: list = field(default_factory=list)
    already_riding: list = field(default_factory=list)
    far: list = field(default_factory=list)  # [(Person, ponto, metros)]
    busy_drivers: list = field(default_factory=list)  # [(Vehicle, conflito)]

    @property
    def passengers(self):
        return sum(route.load for route in self.routes)


def plan(company, schedule, *, max_stops=MAX_STOPS, slack_percent=SLACK_PERCENT, include_managers=False) -> Plan:
    result = Plan(company=company, schedule=schedule)
    points = _public_points(company)
    if not points:
        raise FixedRoutesError(
            "A empresa não tem pontos de encontro. Rode antes: "
            f'python manage.py add_manaus_meeting_points --company "{company.name}"'
        )
    depot = _depot(company)

    riding = _already_riding(company, schedule)
    demand = {}
    for person in active_employees(company).select_related("access_account").order_by("name"):
        if not include_managers and _is_management(person):
            continue
        if person.id in riding:
            result.already_riding.append(person)
            continue
        home = _home(person)
        if home is None:
            result.without_home.append(person)
            continue
        point, meters = _nearest(home, points)
        if meters > FAR_PICKUP_METERS:
            result.far.append((person, point, round(meters)))
        demand.setdefault(point.id, (point, []))[1].append(person)
    items = list(demand.values())
    total = sum(len(riders) for _, riders in items)
    if not total:
        return result

    vehicles = []
    for vehicle in Vehicle.objects.filter(company=company, is_active=True, driver__isnull=False, driver__is_active=True) \
            .select_related("driver").order_by("-capacity", "plate_number"):
        conflict = _driver_conflict(vehicle.driver, schedule, None, None)
        if conflict:
            result.busy_drivers.append((vehicle, conflict))
        else:
            vehicles.append(vehicle)
    limits = [max(1, math.floor(v.capacity * (100 - slack_percent) / 100)) for v in vehicles]
    needed = _vehicles_needed(total, limits)
    if needed is None:
        raise FixedRoutesError(
            f"Não cabe: {total} colaboradores para {sum(limits)} lugares em {len(vehicles)} ônibus livres neste "
            f"horário (com {slack_percent}% de folga). Cadastre mais ônibus com motorista ou reduza a folga."
        )

    best, best_longest = None, math.inf
    for count in range(needed, len(vehicles) + 1):
        groups = _sweep_by_capacity(items, depot, limits[:count], max_stops)
        if groups is None:
            continue
        groups = [route_optimizer.order_route(group, depot) for group in groups]
        longest = max(route_optimizer.route_timeline(group, depot)[0][0] for group in groups)
        if longest < best_longest:
            best, best_longest = groups, longest
        if longest + DEPARTURE_MARGIN_MINUTES <= TARGET_RIDE_MINUTES:
            break
    if best is None:
        raise FixedRoutesError(
            f"Com até {max_stops} paradas por rota, os {len(vehicles)} ônibus livres não cobrem todos os pontos. "
            "Aumente --max-paradas ou cadastre mais ônibus."
        )

    # A rota mais cheia fica com o maior ônibus.
    by_load = sorted(range(len(best)), key=lambda i: -route_optimizer.load_of(best[i]))
    vehicle_of = {group_index: vehicles[rank] for rank, group_index in enumerate(by_load)}
    for index, group in enumerate(best):
        minutes_before, meters = route_optimizer.route_timeline(group, depot)
        result.routes.append(PlannedRoute(index + 1, vehicle_of[index], group, minutes_before, meters))
    return result


@transaction.atomic
def apply(planned: Plan, *, notify_people=False) -> list:
    """Grava as rotas fixas do plano. Com notify_people, avisa passageiros e motoristas no app."""
    schedule, created = planned.schedule, []
    arrival = f"{schedule.time:%H:%M}"
    for item in planned.routes:
        route = Route.objects.create(
            company=planned.company, schedule=schedule, vehicle=item.vehicle,
            name=f"Fixa {arrival} R{item.number} · {item.name_suffix}",
        )
        for sequence, ((point, riders), minutes) in enumerate(zip(item.stops, item.minutes_before)):
            stop = RouteStop.objects.create(route=route, meeting_point_id=point.id, sequence=sequence)
            RoutePassenger.objects.bulk_create(RoutePassenger(person=p, route=route, route_stop=stop) for p in riders)
            if notify_people:
                notify(
                    riders, kind=Notification.Kind.ROUTE_ASSIGNED, title="Nova rota fixa", route=route,
                    message=(
                        f"{route.name}: embarque em {point.name}, cerca de {minutes} min antes da chegada "
                        f"às {arrival} ({schedule.weekdays_label()})."
                    ),
                )
        if notify_people:
            notify(
                [item.vehicle.driver], kind=Notification.Kind.ROUTE_ASSIGNED, title="Nova rota fixa", route=route,
                message=f"{route.name}: {len(item.stops)} paradas, {item.load} passageiros, chegada às {arrival}.",
            )
        created.append(route)
    return created


def remove_existing(company, schedule) -> int:
    """Apaga as rotas fixas do turno (para refazer com --recriar)."""
    routes = Route.objects.filter(company=company, schedule=schedule, service_date__isnull=True)
    count = routes.count()
    routes.delete()
    return count


# ---------------------------------------------------------------------- apoio

def _is_management(person):
    account = getattr(person, "access_account", None)
    return account is not None and account.role in (AccessAccount.Role.MANAGER, AccessAccount.Role.GERENTE)


def _already_riding(company, schedule):
    """Quem já é passageiro de uma rota fixa ativa que roda nos mesmos dias e perto do horário."""
    rides = RoutePassenger.objects.filter(
        route__company=company, is_active=True, route__is_active=True,
        route__service_date__isnull=True, route__schedule__isnull=False,
    ).select_related("route__schedule")
    return {
        ride.person_id for ride in rides
        if _overlaps(ride.route, schedule, None) and _near_in_time(ride.route.schedule, schedule)
    }


def _vehicles_needed(total, limits):
    capacity = 0
    for count, limit in enumerate(limits, start=1):
        capacity += limit
        if capacity >= total:
            return count
    return None


def _sweep_by_capacity(items, depot, limits, max_stops):
    """
    Fatias vizinhas (varredura pelo ângulo a partir da empresa), a i-ésima com até limits[i] pessoas.
    A meta de cada fatia é proporcional ao seu limite, para as rotas ficarem equilibradas. Devolve
    None se sobrar gente (limite de paradas apertado demais para estes ônibus).
    """
    by_point = {id(point): riders for point, riders in items}
    queue = [(point, list(by_point[id(point)])) for point in route_optimizer.sweep_order([p for p, _ in items], depot)]
    remaining = sum(len(riders) for _, riders in queue)
    groups = []
    for index, limit in enumerate(limits):
        if not queue:
            break
        target = math.ceil(remaining * limit / sum(limits[index:]))
        group, load = [], 0
        while queue and len(group) < max_stops:
            point, riders = queue[0]
            room = limit - load
            if len(riders) <= room:
                if group and load >= target:
                    break
                group.append((point, riders))
                load += len(riders)
                queue.pop(0)
            else:
                # Não cabe inteiro: se a rota já está perto da meta, fecha; senão divide o ponto.
                if room <= 0 or (group and load >= 0.75 * target):
                    break
                group.append((point, riders[:room]))
                queue[0] = (point, riders[room:])
                load += room
                break
        groups.append(group)
        remaining -= load
    return groups if not queue else None
