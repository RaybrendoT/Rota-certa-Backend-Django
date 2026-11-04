"""
Ferramentas do assistente de planejamento de rotas avulsas.

Tudo que lê ou altera dados passa por aqui, sempre no escopo da empresa do gestor. O assistente
(IA) só escolhe quais ferramentas chamar; o cálculo das rotas é determinístico (core.route_optimizer) e
as rotas só são gravadas por `approve`, chamado pela API quando o gestor clica em Aprovar.

O rascunho (`PlanningConversation.draft`) guarda apenas IDs e a ordem das paradas:
    {"date": "2026-10-30", "arrival": "06:00", "warnings": [...],
     "routes": [{"driver_id": 7, "vehicle_id": 7,
                 "stops": [{"meeting_point_id": 3, "passenger_ids": [11, 12]}]}]}
Nomes, horários e lotação são recalculados do banco a cada exibição (`describe`).
"""
import math
import unicodedata
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from types import SimpleNamespace

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from core import address_review, pickup_points, route_optimizer
from core.dates import local_today
from core.geo import distance_meters
from core.models import (
    Address,
    MeetingPoint,
    Notification,
    Person,
    PlanningConversation,
    Route,
    RoutePassenger,
    RouteStop,
    Schedule,
    Vehicle,
)
from core.notifications import notify
from core.self_address import company_location, nearest_point

# Um motorista não pega duas rotas cujas chegadas fiquem a menos de 2h30 uma da outra.
ROUTE_WINDOW_MINUTES = 150
MAX_STOPS = 12
# Ponto de embarque mais longe que isso da casa do colaborador vira aviso no plano.
FAR_PICKUP_METERS = 2000
# Rota que sobra quase vazia é distribuída entre as vizinhas.
MIN_ROUTE_LOAD = 8
# Meta de tempo da primeira parada até a empresa: com folga no limite de rotas, usa mais ônibus até caber.
TARGET_RIDE_MINUTES = 90
DEPARTURE_MARGIN_MINUTES = 5
# select_employees só lista nomes de grupos até este tamanho; acima disso, só o resumo.
GROUP_LIST_LIMIT = 60
WEEKDAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
WEEKDAY_NAMES = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


class PlanningError(Exception):
    """Erro de regra de negócio: a mensagem volta para o assistente explicar ao gestor."""


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


def short_point_name(name: str) -> str:
    """'Cidade Nova – Avenida Noel Nutels' → 'Cidade Nova'."""
    for separator in (" – ", " - "):
        if separator in name:
            return name.split(separator, 1)[0].strip()
    return name


def active_employees(company):
    return Person.objects.filter(
        company=company, type=Person.PersonType.EMPLOYEE, is_active=True
    ).prefetch_related(Prefetch("addresses", queryset=Address.objects.filter(is_active=True).order_by("id")))


class EmployeeDirectory:
    """Busca de colaboradores por matrícula, e-mail ou nome (sem acento; partes do nome servem)."""

    def __init__(self, employees):
        employees = list(employees)
        self.by_code = {normalize(p.employee_code): p for p in employees if p.employee_code}
        self.by_email = {
            normalize(p.access_account.email): p for p in employees if hasattr(p, "access_account")
        }
        self.names = [(p, normalize(p.name).split()) for p in employees]

    def by_identifier(self, raw: str):
        """Só matrícula ou e-mail exatos."""
        query = normalize(raw)
        return self.by_code.get(query) or self.by_email.get(query)

    def by_name(self, raw: str):
        """Todos os colaboradores cujo nome tem palavras começando por cada parte buscada."""
        query = normalize(raw)
        tokens = query.split()
        if not tokens:
            return []
        matches = [p for p, parts in self.names if all(any(w.startswith(t) for w in parts) for t in tokens)]
        # Nome completo idêntico vence quem só tem as mesmas palavras em outra ordem.
        exact = [p for p in matches if normalize(p.name) == query]
        return exact if len(exact) == 1 else matches

    def lookup(self, raw: str):
        person = self.by_identifier(raw)
        return [person] if person else self.by_name(raw)


class Planner:
    def __init__(self, conversation: PlanningConversation):
        self.conversation = conversation
        self.company = conversation.company

    # ------------------------------------------------------------------ ferramentas

    def find_employees(self, identifiers):
        """Localiza colaboradores por matrícula, e-mail ou nome (sem acento, partes do nome)."""
        directory = EmployeeDirectory(self._employees_qs().select_related("access_account"))
        found, ambiguous, not_found = {}, [], []
        for raw in identifiers:
            if not normalize(str(raw)):
                continue
            matches = directory.lookup(str(raw))
            if len(matches) == 1:
                found[matches[0].id] = self._employee_info(matches[0])
            elif matches:
                ambiguous.append({"query": raw, "candidates": [self._employee_info(p) for p in matches[:5]]})
            else:
                not_found.append(raw)
        return {
            "found": list(found.values()),
            "ambiguous": ambiguous,
            "not_found": not_found,
        }

    def select_employees(self, group):
        """Confere um grupo inteiro (todos, um turno fixo, bairros) antes de planejar com plan_routes(group=...)."""
        people, description, notes = self._resolve_group(group)
        with_home = [p for p in people if self._home(p) is not None]
        by_point = {}
        if with_home:
            points = self._meeting_points()
            for person in with_home:
                name = short_point_name(self._nearest_point(person, points)[0].name)
                by_point[name] = by_point.get(name, 0) + 1
        result = {
            "group": description,
            "total": len(people),
            "with_address": len(with_home),
            "without_address": [p.name for p in people if self._home(p) is None],
            "by_meeting_point": [
                {"point": name, "people": count}
                for name, count in sorted(by_point.items(), key=lambda item: -item[1])
            ],
            "notes": notes,
        }
        if len(people) <= GROUP_LIST_LIMIT:
            result["people"] = [{"id": p.id, "name": p.name, "code": p.employee_code} for p in people]
        return result

    def available_drivers(self, service_date: str, arrival: str):
        day, arrival_time = self._parse_when(service_date, arrival)
        available, busy = [], []
        for vehicle in self._vehicles():
            conflict = self._conflict(vehicle.driver, day, arrival_time)
            info = {
                "driver_id": vehicle.driver.id,
                "name": vehicle.driver.name,
                "vehicle": f"{vehicle.model} {vehicle.plate_number}",
                "capacity": vehicle.capacity,
            }
            if conflict:
                busy.append({**info, "conflict": conflict})
            else:
                available.append({**info, "routes_that_day": self._routes_that_day(vehicle.driver, day)})
        return {"date": day.isoformat(), "arrival": arrival, "available": available, "busy": busy}

    def plan_routes(self, service_date: str, arrival: str, person_ids, max_routes: int, driver_ids=None, group=None):
        day, arrival_time = self._parse_when(service_date, arrival)
        if max_routes < 1:
            raise PlanningError("max_routes deve ser pelo menos 1.")

        warnings = []
        if group:
            # Grupos grandes não passam IDs pelo modelo: o servidor resolve "todos", turno ou bairros.
            people, _, notes = self._resolve_group(group)
            warnings.extend(notes)
        elif person_ids:
            people = self._people(person_ids)
        else:
            raise PlanningError("Informe person_ids (de find_employees) ou group.")
        homeless = [p for p in people if self._home(p) is None]
        if homeless:
            warnings.append(
                "Sem endereço cadastrado (ficaram fora do plano): " + ", ".join(p.name for p in homeless)
            )
        riders = [p for p in people if self._home(p) is not None]
        if not riders:
            raise PlanningError("Nenhum dos colaboradores informados tem endereço cadastrado.")

        points = self._meeting_points()
        demand = {}
        for person in riders:
            point, meters = self._nearest_point(person, points)
            if meters > FAR_PICKUP_METERS:
                warnings.append(f"{person.name}: ponto de embarque a {meters / 1000:.1f} km de casa ({point.name}).")
            demand.setdefault(point.id, (point, []))[1].append(person)

        vehicles = self._free_vehicles(day, arrival_time, driver_ids)
        vehicles.sort(key=lambda v: v.capacity, reverse=True)
        allowed = min(max_routes, len(vehicles))
        needed = self._routes_needed(len(riders), vehicles)
        if needed is None or needed > allowed:
            capacity = sum(v.capacity for v in vehicles[:allowed])
            raise PlanningError(
                f"Não cabe: {len(riders)} pessoas, mas {allowed} rota(s) com os motoristas livres levam no "
                f"máximo {capacity}. Aumente o número de rotas ou libere motoristas "
                f"({len(vehicles)} disponíveis nesse horário)."
            )

        depot = self._depot()
        groups = self._best_grouping(list(demand.values()), depot, vehicles, needed, allowed)
        longest = max(self._ride_minutes(group, depot) for group in groups)
        if longest > TARGET_RIDE_MINUTES:
            warnings.append(
                f"A rota mais longa leva cerca de {longest} min da primeira parada até a empresa"
                + (" mesmo usando todas as rotas permitidas." if len(groups) >= allowed else ".")
            )

        routes = []
        free = list(vehicles)
        for group in sorted(groups, key=route_optimizer.load_of, reverse=True):
            vehicle = self._closest_vehicle(free, group)
            free.remove(vehicle)
            routes.append({
                "driver_id": vehicle.driver_id,
                "vehicle_id": vehicle.id,
                "stops": [
                    {"meeting_point_id": point.id, "passenger_ids": [p.id for p in members]}
                    for point, members in group
                ],
            })
        self._save({"date": day.isoformat(), "arrival": arrival, "warnings": warnings, "routes": routes})
        return self.describe()

    def move_passengers(self, person_ids, route_number: int):
        draft = self._draft()
        target = self._route_at(draft, route_number)
        people = {p.id: p for p in self._people(person_ids)}
        points = self._meeting_points()
        points_by_id = {p.id: p for p in points}

        for route in draft["routes"]:
            for stop in route["stops"]:
                stop["passenger_ids"] = [pid for pid in stop["passenger_ids"] if pid not in people]
        for person in people.values():
            if self._home(person) is None:
                raise PlanningError(f"{person.name} não tem endereço cadastrado.")
            point, _ = self._nearest_point(person, points)
            stop = next((s for s in target["stops"] if s["meeting_point_id"] == point.id), None)
            if stop is None:
                stop = {"meeting_point_id": point.id, "passenger_ids": []}
                target["stops"].append(stop)
            stop["passenger_ids"].append(person.id)

        vehicle = Vehicle.objects.get(pk=target["vehicle_id"])
        load = sum(len(s["passenger_ids"]) for s in target["stops"])
        if load > vehicle.capacity:
            raise PlanningError(f"A rota {route_number} ficaria com {load} pessoas; o ônibus leva {vehicle.capacity}.")
        if len(target["stops"]) > MAX_STOPS:
            raise PlanningError(f"A rota {route_number} ficaria com mais de {MAX_STOPS} paradas.")

        depot = self._depot()
        for route in draft["routes"]:
            route["stops"] = [s for s in route["stops"] if s["passenger_ids"]]
            items = [(points_by_id[s["meeting_point_id"]], s) for s in route["stops"]]
            route["stops"] = [s for _, s in route_optimizer.order_route(items, depot)] if items else []
        draft["routes"] = [r for r in draft["routes"] if r["stops"]]
        self._save(draft)
        return self.describe()

    def change_driver(self, route_number: int, driver_id: int):
        draft = self._draft()
        route = self._route_at(draft, route_number)
        vehicle = next((v for v in self._vehicles() if v.driver_id == driver_id), None)
        if vehicle is None:
            raise PlanningError("Motorista não encontrado ou sem veículo ativo nesta empresa.")
        if any(r is not route and r["driver_id"] == driver_id for r in draft["routes"]):
            raise PlanningError(f"{vehicle.driver.name} já está em outra rota deste plano.")
        day, arrival_time = self._parse_when(draft["date"], draft["arrival"])
        conflict = self._conflict(vehicle.driver, day, arrival_time)
        if conflict:
            raise PlanningError(f"{vehicle.driver.name} está ocupado nesse horário: {conflict}.")
        load = sum(len(s["passenger_ids"]) for s in route["stops"])
        if load > vehicle.capacity:
            raise PlanningError(f"O ônibus de {vehicle.driver.name} leva {vehicle.capacity}; a rota tem {load} pessoas.")
        route["driver_id"], route["vehicle_id"] = vehicle.driver_id, vehicle.id
        self._save(draft)
        return self.describe()

    def show_plan(self):
        if not self.conversation.draft:
            return {"plan": None, "message": "Ainda não há plano nesta conversa."}
        return self.describe()

    def suggest_pickup_points(self, person_ids=None):
        if person_ids:
            self._people(person_ids)  # valida que são colaboradores desta empresa
        result = pickup_points.suggest_pickup_points(self.company, person_ids or None, max_suggestions=5)
        for suggestion in result["suggestions"]:
            suggestion.pop("person_ids")  # o assistente só precisa dos nomes
        return result

    def pending_address_reviews(self):
        """Endereços enviados pelo app aguardando aprovação do gestor (só leitura)."""
        pending = address_review.review_queryset(self.company).filter(review_status=Address.Review.PENDING)
        items = []
        for address in pending:
            point = nearest_point(self.company, address.latitude, address.longitude)
            items.append({
                "person_id": address.person_id,
                "name": address.person.name,
                "code": address.person.employee_code,
                "new_address": f"{address.street}, {address.number} – {address.neighborhood or address.city}",
                "source": address.get_source_display(),
                "nearest_point": point,
            })
        return {"pending": items}

    def suggest_fixed_routes(self, person_id: int):
        """Rotas fixas que passam perto da casa (o endereço pendente, se houver; senão o atual), com lotação."""
        person = self._people([person_id])[0]
        address = (
            # Consulta direta: person.addresses vem pré-carregado só com os endereços ativos.
            Address.objects.filter(person=person, review_status=Address.Review.PENDING).order_by("-created_at").first()
            or self._home(person)
        )
        if address is None:
            raise PlanningError(f"{person.name} não tem endereço cadastrado nem enviado pelo app.")
        return {
            "person": person.name,
            "address": f"{address.street}, {address.number} – {address.neighborhood or address.city}",
            "address_status": "aguardando aprovação" if address.review_status == Address.Review.PENDING else "atual",
            "current_routes": address_review.current_routes(person),
            "suggestions": address_review.route_suggestions(person, address.latitude, address.longitude),
        }

    # ------------------------------------------------------------------ exibição

    def describe(self):
        draft = self._draft()
        day, arrival_time = self._parse_when(draft["date"], draft["arrival"], allow_past=True)
        depot = self._depot()
        points = {p.id: p for p in self._meeting_points()}
        person_ids = [pid for r in draft["routes"] for s in r["stops"] for pid in s["passenger_ids"]]
        people = {p.id: p for p in Person.objects.filter(id__in=person_ids)}
        vehicles = {v.id: v for v in Vehicle.objects.filter(id__in=[r["vehicle_id"] for r in draft["routes"]]).select_related("driver")}
        arrival_dt = datetime.combine(day, arrival_time)

        routes = []
        for number, route in enumerate(draft["routes"], start=1):
            ordered = [(points[s["meeting_point_id"]], s["passenger_ids"]) for s in route["stops"]]
            minutes_before, meters = route_optimizer.route_timeline(ordered, depot)
            vehicle = vehicles[route["vehicle_id"]]
            departure = arrival_dt - timedelta(minutes=(minutes_before[0] if minutes_before else 0) + DEPARTURE_MARGIN_MINUTES)
            routes.append({
                "number": number,
                "driver": {"id": vehicle.driver_id, "name": vehicle.driver.name},
                "vehicle": {"plate": vehicle.plate_number, "model": vehicle.model, "capacity": vehicle.capacity},
                "load": sum(len(ids) for _, ids in ordered),
                "departure": departure.strftime("%H:%M"),
                "duration_min": (minutes_before[0] if minutes_before else 0) + DEPARTURE_MARGIN_MINUTES,
                "distance_km": round(meters / 1000, 1),
                "stops": [
                    {
                        "order": index + 1,
                        "meeting_point_id": point.id,
                        "point": point.name,
                        "eta": (arrival_dt - timedelta(minutes=minutes)).strftime("%H:%M"),
                        "passengers": [
                            {"id": pid, "name": people[pid].name, "code": people[pid].employee_code} for pid in ids
                        ],
                    }
                    for index, ((point, ids), minutes) in enumerate(zip(ordered, minutes_before))
                ],
            })
        return {
            "date": draft["date"],
            "weekday": WEEKDAY_NAMES[day.weekday()],
            "arrival": draft["arrival"],
            "total_passengers": sum(r["load"] for r in routes),
            "routes": routes,
            "warnings": draft.get("warnings", []),
            "status": self.conversation.status,
            "times_are_estimates": True,
        }

    # ------------------------------------------------------------------ aprovação

    @transaction.atomic
    def approve(self):
        if self.conversation.status != PlanningConversation.Status.DRAFT:
            raise PlanningError("Este plano já foi aprovado.")
        draft = self._draft()
        day, arrival_time = self._parse_when(draft["date"], draft["arrival"])
        plan = self.describe()
        for route in plan["routes"]:
            driver = Person.objects.get(pk=route["driver"]["id"])
            conflict = self._conflict(driver, day, arrival_time)
            if conflict:
                raise PlanningError(f"{driver.name} ficou ocupado nesse horário depois do plano: {conflict}.")

        schedule = Schedule.objects.filter(company=self.company, time=arrival_time, is_active=True).first()
        if schedule is None:
            schedule = Schedule.objects.create(
                company=self.company, time=arrival_time, weekdays=[WEEKDAY_CODES[day.weekday()]]
            )
        created = []
        for planned, stored in zip(plan["routes"], draft["routes"]):
            labels = list(dict.fromkeys(short_point_name(s["point"]) for s in planned["stops"]))
            route = Route.objects.create(
                company=self.company,
                name=f"Extra {day:%d/%m} {arrival_time:%H:%M} R{planned['number']} · " + " · ".join(labels[:2]),
                vehicle_id=stored["vehicle_id"],
                schedule=schedule,
                service_date=day,
            )
            for sequence, stop in enumerate(planned["stops"]):
                route_stop = RouteStop.objects.create(
                    route=route, meeting_point_id=stop["meeting_point_id"], sequence=sequence
                )
                RoutePassenger.objects.bulk_create(
                    RoutePassenger(person_id=p["id"], route=route, route_stop=route_stop) for p in stop["passengers"]
                )
                notify(
                    Person.objects.filter(id__in=[p["id"] for p in stop["passengers"]]),
                    kind=Notification.Kind.ROUTE_ASSIGNED,
                    title=f"Rota extra em {day:%d/%m}",
                    message=f"Embarque às {stop['eta']} em {stop['point']}. Chegada às {draft['arrival']}.",
                    route=route,
                    data={"service_date": day.isoformat()},
                )
            notify(
                [Person.objects.get(pk=planned["driver"]["id"])],
                kind=Notification.Kind.ROUTE_ASSIGNED,
                title=f"Nova rota em {day:%d/%m}",
                message=(
                    f"{route.name}: saída às {planned['departure']}, {len(planned['stops'])} paradas, "
                    f"{planned['load']} passageiros."
                ),
                route=route,
                data={"service_date": day.isoformat()},
            )
            created.append(route)

        self.conversation.routes.set(created)
        self.conversation.status = PlanningConversation.Status.APPROVED
        self.conversation.approved_at = timezone.now()
        self.conversation.save(update_fields=["status", "approved_at", "updated_at"])
        return created

    # ------------------------------------------------------------------ apoio

    def _employees_qs(self):
        return active_employees(self.company)

    def _employee_info(self, person):
        home = self._home(person)
        return {
            "id": person.id,
            "name": person.name,
            "code": person.employee_code,
            "address": f"{home.street}, {home.number}" if home else None,
        }

    @staticmethod
    def _home(person):
        addresses = list(person.addresses.all())
        return addresses[0] if addresses else None

    def _people(self, person_ids):
        ids = list(dict.fromkeys(int(pid) for pid in person_ids))
        people = list(self._employees_qs().filter(id__in=ids))
        missing = set(ids) - {p.id for p in people}
        if missing:
            raise PlanningError(
                f"IDs que não são colaboradores ativos desta empresa: {sorted(missing)}. Use find_employees."
            )
        return people

    def _resolve_group(self, group):
        """(colaboradores, descrição, observações) de {"scope", "schedule_arrival", "bairros", "exclude"}."""
        scope = group.get("scope")
        people = list(self._employees_qs().select_related("access_account").order_by("name"))
        if scope == "all":
            description = "todos os colaboradores ativos da empresa"
        elif scope == "schedule":
            arrival = group.get("schedule_arrival")
            try:
                arrival_time = time.fromisoformat(arrival or "")
            except ValueError:
                raise PlanningError("Para scope=schedule informe schedule_arrival no formato HH:MM.") from None
            routes = Route.objects.filter(
                company=self.company, is_active=True, service_date__isnull=True, schedule__time=arrival_time
            )
            if not routes.exists():
                times = sorted({
                    t.strftime("%H:%M")
                    for t in Route.objects.filter(
                        company=self.company, is_active=True, service_date__isnull=True, schedule__isnull=False
                    ).values_list("schedule__time", flat=True)
                })
                raise PlanningError(
                    f"Não há rotas fixas com chegada às {arrival}. Turnos com rota fixa: {', '.join(times) or 'nenhum'}."
                )
            riders = set(RoutePassenger.objects.filter(route__in=routes).values_list("person_id", flat=True))
            people = [p for p in people if p.id in riders]
            description = f"passageiros das rotas fixas com chegada às {arrival}"
        elif scope == "bairros":
            wanted = [normalize(b) for b in group.get("bairros") or [] if normalize(b)]
            if not wanted:
                raise PlanningError("Para scope=bairros informe a lista bairros.")
            points = self._meeting_points()

            def in_bairro(person):
                point_name = normalize(short_point_name(self._nearest_point(person, points)[0].name))
                return any(point_name.startswith(b) for b in wanted)

            people = [p for p in people if self._home(p) is not None and in_bairro(p)]
            if not people:
                names = sorted({short_point_name(p.name) for p in points})
                raise PlanningError(
                    "Ninguém embarca nesses bairros. Bairros com ponto de encontro: " + ", ".join(names) + "."
                )
            description = "quem embarca no ponto de encontro de " + ", ".join(group["bairros"])
        else:
            raise PlanningError("scope deve ser all, schedule ou bairros.")

        notes = []
        exclude = [e for e in group.get("exclude") or [] if normalize(str(e))]
        if exclude:
            directory = EmployeeDirectory(people)
            removed = set()
            for raw in exclude:
                matches = directory.lookup(str(raw))
                if len(matches) == 1:
                    removed.add(matches[0].id)
                elif matches:
                    notes.append(f"Exceção '{raw}' é ambígua ({', '.join(p.name for p in matches[:5])}); não foi removida.")
                else:
                    notes.append(f"Exceção '{raw}' não está no grupo.")
            people = [p for p in people if p.id not in removed]
            if removed:
                description += f", exceto {len(removed)} pessoa(s)"
        if not people:
            raise PlanningError("O grupo ficou vazio.")
        return people, description, notes

    def _meeting_points(self):
        points = [
            SimpleNamespace(id=mp.id, name=mp.name, lat=float(mp.address.latitude), lng=float(mp.address.longitude))
            for mp in MeetingPoint.objects.filter(company=self.company, is_active=True, home_of__isnull=True).select_related("address")
            if mp.address.latitude is not None
        ]
        if not points:
            raise PlanningError("A empresa não tem pontos de encontro cadastrados para usar como paradas.")
        return points

    def _nearest_point(self, person, points):
        home = self._home(person)
        lat, lng = float(home.latitude), float(home.longitude)
        point = min(points, key=lambda p: distance_meters(lat, lng, p.lat, p.lng))
        return point, distance_meters(lat, lng, point.lat, point.lng)

    def _depot(self):
        address = company_location(self.company)
        if address is None:
            raise PlanningError("A empresa não tem endereço cadastrado (destino das rotas).")
        return SimpleNamespace(lat=float(address.latitude), lng=float(address.longitude))

    def _vehicles(self):
        return list(
            Vehicle.objects.filter(company=self.company, is_active=True, driver__isnull=False, driver__is_active=True)
            .select_related("driver")
            .order_by("driver__name")
        )

    def _free_vehicles(self, day, arrival_time, driver_ids):
        vehicles = [v for v in self._vehicles() if not self._conflict(v.driver, day, arrival_time)]
        if driver_ids:
            wanted = set(driver_ids)
            unknown = wanted - {v.driver_id for v in vehicles}
            if unknown:
                raise PlanningError(
                    f"Motoristas indisponíveis nesse horário ou inexistentes: {sorted(unknown)}. "
                    "Consulte available_drivers."
                )
            vehicles = [v for v in vehicles if v.driver_id in wanted]
        if not vehicles:
            raise PlanningError("Nenhum motorista livre nesse dia e horário.")
        return vehicles

    def _best_grouping(self, items, depot, vehicles, needed, allowed):
        """
        Testa de `needed` (mínimo pela capacidade) até `allowed` rotas e fica com o menor número de
        ônibus cuja rota mais longa caiba em TARGET_RIDE_MINUTES; se nenhum couber, o de menor
        viagem mais longa. Mais ônibus = viagens mais curtas para todo mundo.
        """
        best, best_longest = None, math.inf
        for count in range(needed, allowed + 1):
            max_load = min(v.capacity for v in vehicles[:count])
            groups = route_optimizer.sweep_groups(
                items, depot, route_count=count, max_load=max_load, max_stops=MAX_STOPS
            )
            # Só some com sobras de verdade: metade da lotação média desta tentativa (no máximo MIN_ROUTE_LOAD).
            min_load = min(MIN_ROUTE_LOAD, math.ceil(route_optimizer.load_of(items) / count / 2))
            groups = route_optimizer.absorb_small_groups(
                groups, min_load=min_load, max_load=max_load, max_stops=MAX_STOPS
            )
            if len(groups) > allowed:
                continue
            groups = [route_optimizer.order_route(group, depot) for group in groups]
            longest = max(self._ride_minutes(group, depot) for group in groups)
            if longest < best_longest:
                best, best_longest = groups, longest
            if longest <= TARGET_RIDE_MINUTES:
                break
        if best is None:
            raise PlanningError(
                f"Com até {MAX_STOPS} paradas por ônibus, o plano precisa de mais que as {allowed} rotas permitidas."
            )
        return best

    @staticmethod
    def _ride_minutes(group, depot):
        minutes_before, _ = route_optimizer.route_timeline(group, depot)
        return minutes_before[0] if minutes_before else 0

    @staticmethod
    def _routes_needed(total, vehicles):
        """Menor número de ônibus (dos maiores para os menores) que comporta todo mundo."""
        capacity = 0
        for count, vehicle in enumerate(vehicles, start=1):
            capacity += vehicle.capacity
            if capacity >= total:
                return count
        return None

    def _closest_vehicle(self, vehicles, group):
        load = route_optimizer.load_of(group)
        fitting = [v for v in vehicles if v.capacity >= load] or [max(vehicles, key=lambda v: v.capacity)]
        first_stop = group[0][0]

        def distance_from_home(vehicle):
            home = Address.objects.filter(person=vehicle.driver, is_active=True).order_by("id").first()
            if home is None:
                return math.inf
            return distance_meters(float(home.latitude), float(home.longitude), first_stop.lat, first_stop.lng)

        return min(fitting, key=distance_from_home)

    def _day_routes(self, driver, day):
        weekday = WEEKDAY_CODES[day.weekday()]
        routes = Route.objects.filter(
            vehicle__driver=driver, is_active=True, schedule__isnull=False
        ).filter(Q(service_date=day) | Q(service_date__isnull=True)).select_related("schedule")
        return [r for r in routes if r.service_date == day or weekday in (r.schedule.weekdays or [])]

    def _routes_that_day(self, driver, day):
        return [f"{r.schedule.time:%H:%M} {r.name}" for r in sorted(self._day_routes(driver, day), key=lambda r: r.schedule.time)]

    def _conflict(self, driver, day, arrival_time):
        target = arrival_time.hour * 60 + arrival_time.minute
        for route in self._day_routes(driver, day):
            other = route.schedule.time.hour * 60 + route.schedule.time.minute
            if abs(other - target) < ROUTE_WINDOW_MINUTES:
                return f"{route.schedule.time:%H:%M} {route.name}"
        return None

    def _parse_when(self, service_date, arrival, allow_past=False):
        try:
            day = date_cls.fromisoformat(service_date)
        except (TypeError, ValueError):
            raise PlanningError("Data inválida; use AAAA-MM-DD.") from None
        try:
            arrival_time = time.fromisoformat(arrival)
        except (TypeError, ValueError):
            raise PlanningError("Horário inválido; use HH:MM.") from None
        if not allow_past and day < local_today():
            raise PlanningError(f"A data {day:%d/%m/%Y} já passou.")
        return day, arrival_time

    def _draft(self):
        if not self.conversation.draft:
            raise PlanningError("Ainda não há plano; use plan_routes primeiro.")
        return self.conversation.draft

    @staticmethod
    def _route_at(draft, route_number):
        if not 1 <= route_number <= len(draft["routes"]):
            raise PlanningError(f"Rota {route_number} não existe; o plano tem {len(draft['routes'])} rota(s).")
        return draft["routes"][route_number - 1]

    def _save(self, draft):
        if self.conversation.status != PlanningConversation.Status.DRAFT:
            raise PlanningError("Este plano já foi aprovado; abra um novo planejamento para mudanças.")
        self.conversation.draft = draft
        self.conversation.save(update_fields=["draft", "updated_at"])
