"""
Simula uma empresa do Polo Industrial de Manaus com 1.000 colaboradores em três turnos.

Como as rotas são montadas (por turno):
1. Cada colaborador mora num bairro real (peso por bairro em core/seed_data/manaus_bairros.py)
   e embarca no ponto de parada do bairro mais perto de casa.
2. Os pontos são ordenados pelo ângulo em relação à fábrica e agrupados em "fatias" vizinhas
   até lotar o ônibus (método de varredura / sweep, heurística clássica de roteirização).
   Assim cada rota cobre bairros próximos entre si, numa mesma direção a partir da fábrica.
   Limites: 42 pessoas (ônibus de 44 lugares) e 10 paradas por rota; um bairro com pouca gente
   no turno usa um ponto só, e rota que sobra quase vazia é distribuída entre as vizinhas.
3. Dentro da rota, a ordem das paradas sai por inserção mais barata: o ônibus começa longe da
   fábrica e cada parada entra onde menos aumenta o trajeto até a fábrica.

Motoristas: um por rota do maior turno. Nos outros turnos cada rota vai para o motorista
que atende a mesma região, então todo motorista tem rotas em pelo menos dois horários
(1º e 2º turno) e parte deles também no administrativo.

Rodar de novo recria as rotas, paradas e passageiros da empresa; pessoas, contas e
veículos são reaproveitados.
"""
import math
import random
from datetime import time
from decimal import Decimal
from typing import NamedTuple

from django.core.management.base import BaseCommand
from django.db import transaction

from core import route_optimizer
from core.geo import distance_meters
from core.management.commands.seed_manaus_demo import FIRST_NAMES, LAST_NAMES
from core.models import (
    AccessAccount,
    Address,
    Company,
    MeetingPoint,
    Person,
    Route,
    RoutePassenger,
    RouteStop,
    Schedule,
    Vehicle,
)
from core.seed_data.manaus_bairros import BAIRROS, COMPANY_ADDRESS

COMPANY_NAME = "Norte Componentes da Amazônia"
COMPANY_DOMAIN = "nortecomponentes.com.br"
MANAGER_CODE = "GESTOR01"
GENERIC_ZIP = "69000-000"

MON_FRI = ["MON", "TUE", "WED", "THU", "FRI"]
MON_SAT = MON_FRI + ["SAT"]


class Shift(NamedTuple):
    code: str
    label: str
    arrival: time
    weekdays: list[str]
    share: float


SHIFTS = [
    Shift("T1", "1º turno", time(6, 0), MON_SAT, 0.45),
    Shift("ADM", "Administrativo", time(8, 0), MON_FRI, 0.20),
    Shift("T2", "2º turno", time(14, 0), MON_SAT, 0.35),
]

BUS_CAPACITY = 44
# Folga para remanejamentos e visitantes.
MAX_LOAD = 42
# Ocupação-alvo ao decidir quantas rotas o turno precisa (equilibra a lotação entre elas).
TARGET_FILL = 0.9
# Rota com paradas demais vira um tour pela cidade: acima disso abre outra rota.
MAX_STOPS = 10
# Ponto com menos gente que isso no turno é unido ao outro ponto do mesmo bairro.
MIN_STOP_RIDERS = 6
# Rota com menos gente que isso tem as paradas redistribuídas entre as rotas vizinhas.
MIN_ROUTE_LOAD = 15
BUS_MODELS = ["Marcopolo Torino", "Mercedes-Benz OF-1721", "Volkswagen 17.230 OD", "Caio Apache Vip"]

# Casas espalhadas até ~650 m do centro do bairro.
HOME_JITTER_DEGREES = 0.006


class Passenger(NamedTuple):
    person: Person
    bairro: str
    lat: float
    lng: float


class Stop(NamedTuple):
    point: MeetingPoint
    bairro: str
    lat: float
    lng: float


class Command(BaseCommand):
    help = "Simula uma empresa de Manaus com 1.000 colaboradores, três turnos e rotas por região."

    def add_arguments(self, parser):
        parser.add_argument("--employees", type=int, default=1000, help="Total de colaboradores (padrão 1000).")
        parser.add_argument("--seed", type=int, default=2026, help="Semente aleatória (mesma semente = mesmos dados).")
        parser.add_argument(
            "--demo-password",
            default="",
            help="Se informada, vira a senha (sem troca obrigatória) de um motorista e dois colaboradores demo.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(options["seed"])
        company = self.create_company()
        schedules = self.create_schedules(company)
        stops_by_bairro = self.create_meeting_points(company)

        employees = self.create_employees(company, options["employees"], rng)
        by_shift = self.split_by_shift(employees, rng)

        plans = {
            shift.code: self.plan_routes(by_shift[shift.code], stops_by_bairro)
            for shift in SHIFTS
        }
        drivers_by_shift = self.assign_drivers(plans)
        vehicles = self.create_drivers_and_vehicles(company, plans, drivers_by_shift, rng)
        routes = self.create_routes(company, schedules, plans, drivers_by_shift, vehicles)
        manager = self.create_manager(company)

        if options["demo_password"]:
            self.set_demo_passwords(routes, manager, options["demo_password"])
        self.report(routes)

    # ---------------------------------------------------------------- cadastro base

    def create_company(self):
        company, _ = Company.objects.update_or_create(
            name=COMPANY_NAME, defaults={"email_domain": COMPANY_DOMAIN, "is_active": True}
        )
        # Recria rotas e endereços da empresa (os pontos de encontro caem junto, em cascata).
        Route.objects.filter(company=company).delete()
        Address.objects.filter(company=company, person=None).delete()
        # Criado primeiro: é o destino que a navegação do motorista usa (endereço da empresa fora das paradas).
        Address.objects.create(
            company=company,
            street=COMPANY_ADDRESS.street,
            number=COMPANY_ADDRESS.number,
            city="Manaus",
            state="AM",
            zip_code="69075-000",
            latitude=Decimal(f"{COMPANY_ADDRESS.lat:.6f}"),
            longitude=Decimal(f"{COMPANY_ADDRESS.lng:.6f}"),
        )
        return company

    def create_schedules(self, company):
        schedules = {}
        for shift in SHIFTS:
            schedule, _ = Schedule.objects.update_or_create(
                company=company, time=shift.arrival, defaults={"weekdays": shift.weekdays, "is_active": True}
            )
            schedules[shift.code] = schedule
        return schedules

    def create_meeting_points(self, company):
        MeetingPoint.objects.filter(company=company).delete()
        stops_by_bairro = {}
        for bairro in BAIRROS:
            stops_by_bairro[bairro.name] = []
            for stop in bairro.stops:
                address = Address.objects.create(
                    company=company,
                    street=stop.street,
                    number=stop.number,
                    city="Manaus",
                    state="AM",
                    zip_code=GENERIC_ZIP,
                    latitude=Decimal(f"{stop.lat:.6f}"),
                    longitude=Decimal(f"{stop.lng:.6f}"),
                )
                point = MeetingPoint.objects.create(
                    company=company, name=f"{bairro.name} – {stop.street}", address=address
                )
                stops_by_bairro[bairro.name].append(Stop(point, bairro.name, stop.lat, stop.lng))
        return stops_by_bairro

    def create_employees(self, company, total, rng):
        counts = distribute(total, [b.weight for b in BAIRROS])
        homes = [bairro for bairro, count in zip(BAIRROS, counts) for _ in range(count)]
        rng.shuffle(homes)

        employees = []
        addresses = []
        for index, bairro in enumerate(homes):
            code = f"NC{index + 1:04d}"
            person, _ = Person.objects.update_or_create(
                company=company,
                employee_code=code,
                defaults={
                    "type": Person.PersonType.EMPLOYEE,
                    "name": random_name(rng),
                    "phone": f"+55929{91000000 + index:08d}",
                    "license_number": None,
                    "is_active": True,
                },
            )
            lat = bairro.lat + rng.uniform(-HOME_JITTER_DEGREES, HOME_JITTER_DEGREES)
            lng = bairro.lng + rng.uniform(-HOME_JITTER_DEGREES, HOME_JITTER_DEGREES)
            employees.append(Passenger(person, bairro.name, lat, lng))
            addresses.append(home_address(person, bairro, lat, lng, rng))

        Address.objects.filter(person__in=[e.person for e in employees]).delete()
        Address.objects.bulk_create(addresses)
        return employees

    def split_by_shift(self, employees, rng):
        counts = distribute(len(employees), [shift.share for shift in SHIFTS])
        codes = [shift.code for shift, count in zip(SHIFTS, counts) for _ in range(count)]
        rng.shuffle(codes)
        by_shift = {shift.code: [] for shift in SHIFTS}
        for employee, code in zip(employees, codes):
            by_shift[code].append(employee)
        return by_shift

    # ---------------------------------------------------------------- roteirização

    def plan_routes(self, passengers, stops_by_bairro):
        """Lista de rotas; cada rota = lista ordenada de (Stop, [Passenger])."""
        demand = {}
        for passenger in passengers:
            stop = min(
                stops_by_bairro[passenger.bairro],
                key=lambda s: distance_meters(passenger.lat, passenger.lng, s.lat, s.lng),
            )
            demand.setdefault(stop.point.id, (stop, []))[1].append(passenger)
        merge_small_stops(demand)

        groups = route_optimizer.sweep_groups(
            list(demand.values()),
            COMPANY_ADDRESS,
            route_count=math.ceil(len(passengers) / (MAX_LOAD * TARGET_FILL)),
            max_load=MAX_LOAD,
            max_stops=MAX_STOPS,
        )
        groups = route_optimizer.absorb_small_groups(groups, min_load=MIN_ROUTE_LOAD, max_load=MAX_LOAD, max_stops=MAX_STOPS)
        return [route_optimizer.order_route(group, COMPANY_ADDRESS) for group in groups]

    def assign_drivers(self, plans):
        """
        Motorista k faz a k-ª rota do maior turno. Nos outros turnos cada rota vai para um motorista
        livre naquele turno, primeiro para quem ainda só tem um horário, depois para quem atende a
        região mais parecida (ângulo a partir da fábrica).
        """
        main = max(plans, key=lambda code: len(plans[code]))
        anchor = [route_optimizer.mean_angle(route, COMPANY_ADDRESS) for route in plans[main]]
        drivers = {main: list(range(len(plans[main])))}
        has_second_shift = set()
        for code in sorted((c for c in plans if c != main), key=lambda c: -len(plans[c])):
            free = set(range(len(anchor)))
            drivers[code] = []
            for route in plans[code]:
                angle = route_optimizer.mean_angle(route, COMPANY_ADDRESS)
                best = min(free, key=lambda k: (k in has_second_shift, route_optimizer.angular_distance(anchor[k], angle)))
                free.discard(best)
                has_second_shift.add(best)
                drivers[code].append(best)
        return drivers

    # ---------------------------------------------------------------- persistência

    def create_drivers_and_vehicles(self, company, plans, drivers_by_shift, rng):
        main = max(plans, key=lambda code: len(plans[code]))
        vehicles = []
        for k, route in enumerate(plans[main]):
            first_stop = route[0][0]
            bairro = next(b for b in BAIRROS if b.name == first_stop.bairro)
            driver, _ = Person.objects.update_or_create(
                company=company,
                license_number=f"NCM{k + 1:03d}",
                defaults={
                    "type": Person.PersonType.DRIVER,
                    "name": random_name(rng),
                    "phone": f"+55929{92000000 + k:08d}",
                    "employee_code": None,
                    "is_active": True,
                },
            )
            Address.objects.filter(person=driver).delete()
            home_address(driver, bairro, bairro.lat, bairro.lng, rng).save()
            vehicle, _ = Vehicle.objects.update_or_create(
                plate_number=f"NCA{1001 + k}",
                defaults={
                    "company": company,
                    "driver": driver,
                    "model": BUS_MODELS[k % len(BUS_MODELS)],
                    "capacity": BUS_CAPACITY,
                    "is_active": True,
                },
            )
            vehicles.append(vehicle)
        return vehicles

    def create_routes(self, company, schedules, plans, drivers_by_shift, vehicles):
        created = []
        passengers = []
        for shift in SHIFTS:
            for number, (route_plan, driver_index) in enumerate(
                zip(plans[shift.code], drivers_by_shift[shift.code]), start=1
            ):
                bairros = list(dict.fromkeys(stop.bairro for stop, _ in route_plan))
                route = Route.objects.create(
                    company=company,
                    name=f"{shift.code}-{number:02d} {route_label(bairros)}",
                    vehicle=vehicles[driver_index],
                    schedule=schedules[shift.code],
                )
                for sequence, (stop, riders) in enumerate(route_plan):
                    route_stop = RouteStop.objects.create(route=route, meeting_point=stop.point, sequence=sequence)
                    passengers.extend(
                        RoutePassenger(person=rider.person, route=route, route_stop=route_stop) for rider in riders
                    )
                created.append(route)
        RoutePassenger.objects.bulk_create(passengers)
        return created

    def create_manager(self, company):
        """Gestor da empresa (painel e assistente de planejamento); não entra nas rotas."""
        person, _ = Person.objects.update_or_create(
            company=company,
            employee_code=MANAGER_CODE,
            defaults={
                "type": Person.PersonType.EMPLOYEE,
                "name": "Gestão de Transporte Norte",
                "phone": "+5592990009000",
                "license_number": None,
                "is_active": True,
            },
        )
        account = person.access_account
        if account.role != AccessAccount.Role.MANAGER:
            account.role = AccessAccount.Role.MANAGER
            account.save(update_fields=["role", "updated_at"])
        return person

    def set_demo_passwords(self, routes, manager, password):
        driver = max(
            {route.vehicle.driver for route in routes},
            key=lambda d: Route.objects.filter(vehicle__driver=d).count(),
        )
        demo_people = [manager, driver]
        for code in ("T1", "ADM"):
            route = next(r for r in routes if r.name.startswith(f"{code}-"))
            demo_people.append(route.route_passengers.order_by("id").first().person)
        for person in demo_people:
            account = person.access_account
            account.set_password(password)
            account.must_change_password = False
            account.save(update_fields=["password_hash", "must_change_password", "updated_at"])
        self.stdout.write("Contas demo (senha informada em --demo-password, sem troca obrigatória):")
        for person in demo_people:
            routes_text = ", ".join(
                f"{r.schedule.time:%H:%M} {r.name}"
                for r in Route.objects.filter(route_passengers__person=person).select_related("schedule")
            ) or ", ".join(
                f"{r.schedule.time:%H:%M} {r.name}"
                for r in Route.objects.filter(vehicle__driver=person).select_related("schedule").order_by("schedule__time")
            )
            label = "Gestor" if person == manager else person.get_type_display()
            self.stdout.write(f"  {label:12} {person.access_account.email:34} {routes_text or 'painel e assistente'}")

    def report(self, routes):
        self.stdout.write("")
        for shift in SHIFTS:
            shift_routes = [r for r in routes if r.name.startswith(f"{shift.code}-")]
            loads = [r.route_passengers.count() for r in shift_routes]
            self.stdout.write(
                f"{shift.label} ({shift.arrival:%H:%M}): {len(shift_routes)} rotas, "
                f"{sum(loads)} colaboradores, ocupação {min(loads)}–{max(loads)} de {BUS_CAPACITY} lugares"
            )
        drivers = {}
        for route in routes:
            drivers.setdefault(route.vehicle.driver, []).append(route)
        self.stdout.write(f"\n{len(drivers)} motoristas:")
        for driver, driver_routes in sorted(drivers.items(), key=lambda item: item[0].license_number):
            schedule_text = " | ".join(
                f"{r.schedule.time:%H:%M} {r.name}" for r in sorted(driver_routes, key=lambda r: r.schedule.time)
            )
            self.stdout.write(f"  {driver.license_number} {driver.name:28} {schedule_text}")
        self.stdout.write(self.style.SUCCESS(f"\n{COMPANY_NAME}: {len(routes)} rotas criadas."))


# -------------------------------------------------------------------- funções puras


def distribute(total, weights):
    """Divide total proporcionalmente aos pesos (maiores restos), somando exatamente total."""
    weight_sum = sum(weights)
    raw = [total * w / weight_sum for w in weights]
    counts = [int(r) for r in raw]
    for index in sorted(range(len(raw)), key=lambda i: raw[i] - counts[i], reverse=True)[: total - sum(counts)]:
        counts[index] += 1
    return counts


def merge_small_stops(demand):
    """Bairro com pouca gente no turno usa um ponto só: o mais movimentado recebe os do ponto vazio."""
    by_bairro = {}
    for stop_id, (stop, riders) in demand.items():
        by_bairro.setdefault(stop.bairro, []).append(stop_id)
    for stop_ids in by_bairro.values():
        if len(stop_ids) < 2:
            continue
        stop_ids.sort(key=lambda sid: len(demand[sid][1]))
        smallest, busiest = stop_ids[0], stop_ids[-1]
        if len(demand[smallest][1]) < MIN_STOP_RIDERS:
            demand[busiest][1].extend(demand.pop(smallest)[1])


def route_label(bairros):
    if len(bairros) <= 3:
        return " · ".join(bairros)
    return f"{bairros[0]} · {bairros[1]} · +{len(bairros) - 2} bairros"


def random_name(rng):
    middle, last = rng.sample(LAST_NAMES, 2)
    return f"{rng.choice(FIRST_NAMES)} {middle} {last}"


def home_address(person, bairro, lat, lng, rng):
    street = rng.choice(bairro.stops).street
    return Address(
        person=person,
        street=street,
        number=str(rng.randint(10, 2999)),
        city="Manaus",
        state="AM",
        zip_code=GENERIC_ZIP,
        latitude=Decimal(f"{lat:.6f}"),
        longitude=Decimal(f"{lng:.6f}"),
    )
