from datetime import time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import (
    AccessAccount,
    Address,
    Company,
    MeetingPoint,
    Person,
    Route,
    RouteEvent,
    RoutePassenger,
    RouteStop,
    Schedule,
    Vehicle,
)

COMPANIES = [
    {
        "name": "Polo Digital Manaus",
        "email_domain": "polodigitalmanaus.com",
        "street": "Avenida Mario Ypiranga",
        "number": "1200",
        "zip_code": "69057-002",
        "lat": "-3.091390",
        "lng": "-60.018890",
    },
    {
        "name": "Amazonia Logistica Norte",
        "email_domain": "amazonialogistica.com",
        "street": "Avenida Buriti",
        "number": "4500",
        "zip_code": "69075-000",
        "lat": "-3.128210",
        "lng": "-59.961330",
    },
    {
        "name": "Rio Negro Alimentos",
        "email_domain": "rionegroalimentos.com",
        "street": "Avenida Torquato Tapajos",
        "number": "3100",
        "zip_code": "69048-660",
        "lat": "-3.047500",
        "lng": "-60.021700",
    },
    {
        "name": "Distrito Industrial Tech",
        "email_domain": "distritotech.com",
        "street": "Avenida dos Oitis",
        "number": "710",
        "zip_code": "69075-842",
        "lat": "-3.129780",
        "lng": "-59.967430",
    },
    {
        "name": "Ponta Negra Servicos",
        "email_domain": "pontanegraservicos.com",
        "street": "Avenida Coronel Teixeira",
        "number": "5800",
        "zip_code": "69037-000",
        "lat": "-3.064830",
        "lng": "-60.102210",
    },
]


NEIGHBORHOODS = [
    ("Centro", "Rua Henrique Martins", "-3.133120", "-60.023980"),
    ("Adrianopolis", "Rua Salvador", "-3.100870", "-60.012700"),
    ("Aleixo", "Avenida Andre Araujo", "-3.082420", "-60.000100"),
    ("Chapada", "Rua Recife", "-3.095680", "-60.034330"),
    ("Flores", "Avenida Torquato Tapajos", "-3.060910", "-60.025900"),
    ("Parque Dez", "Rua do Comercio", "-3.074530", "-60.006490"),
    ("Coroado", "Avenida Cosme Ferreira", "-3.091280", "-59.974560"),
    ("Sao Jose", "Avenida Autaz Mirim", "-3.085300", "-59.931350"),
    ("Educandos", "Rua Inocencio de Araujo", "-3.142500", "-60.006800"),
    ("Compensa", "Avenida Brasil", "-3.108600", "-60.062800"),
    ("Ponta Negra", "Avenida do Turismo", "-3.063300", "-60.107500"),
    ("Cidade Nova", "Avenida Noel Nutels", "-3.025700", "-59.984600"),
    ("Taruma", "Avenida do Turismo", "-3.012500", "-60.067600"),
    ("Japiim", "Avenida Tefe", "-3.124600", "-59.994100"),
    ("Cachoeirinha", "Rua Borba", "-3.122300", "-60.014100"),
]


FIRST_NAMES = [
    "Ana",
    "Bruno",
    "Carla",
    "Diego",
    "Elisa",
    "Fabio",
    "Gabriela",
    "Henrique",
    "Isabela",
    "Joao",
    "Karina",
    "Lucas",
    "Mariana",
    "Nicolas",
    "Olivia",
    "Paulo",
    "Renata",
    "Samuel",
    "Tatiana",
    "Vinicius",
    "Bianca",
    "Carlos",
    "Daniela",
    "Eduardo",
    "Fernanda",
    "Gustavo",
    "Helena",
    "Igor",
    "Juliana",
    "Leonardo",
    "Monica",
    "Rafael",
    "Sabrina",
    "Thiago",
    "Vanessa",
    "William",
    "Aline",
    "Cesar",
    "Debora",
    "Everton",
    "Patricia",
    "Roberto",
    "Simone",
    "Valter",
    "Yasmin",
    "Marcelo",
    "Leticia",
    "Otavio",
    "Priscila",
    "Wesley",
]


LAST_NAMES = [
    "Silva",
    "Souza",
    "Oliveira",
    "Santos",
    "Lima",
    "Costa",
    "Pereira",
    "Almeida",
    "Ribeiro",
    "Nascimento",
]


VEHICLE_MODELS = [
    "Mercedes-Benz Sprinter",
    "Renault Master",
    "Volare Fly 10",
    "Iveco Daily",
    "Marcopolo Senior",
    "Volkswagen 9.160",
    "Fiat Ducato",
    "Mercedes-Benz OF-1721",
    "Agrale Marrua AM200",
    "Volare Attack 8",
]


class Command(BaseCommand):
    help = "Popula o banco com dados demo de Manaus para simular rotas e passageiros."

    def add_arguments(self, parser):
        parser.add_argument(
            "--people",
            type=int,
            default=50,
            help="Total de pessoas demo a garantir. Minimo recomendado: 50.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        people_total = max(options["people"], 50)
        driver_total = max(10, people_total // 5)
        employee_total = people_total - driver_total

        companies = self.create_companies()
        self.create_schedules(companies)
        meeting_points = self.create_meeting_points(companies)
        drivers = self.create_people(companies, driver_total, Person.PersonType.DRIVER)
        employees = self.create_people(companies, employee_total, Person.PersonType.EMPLOYEE)
        vehicles = self.create_vehicles(companies, drivers)
        routes = self.create_routes(companies, vehicles, meeting_points)
        stops_by_route = self.create_route_stops(routes, meeting_points)
        self.create_route_passengers(employees, routes, stops_by_route)
        self.create_route_events(routes)
        manager = self.create_demo_manager(companies[0])

        self.stdout.write(
            f"Gestor demo: {manager.access_account.email} "
            "(senha inicial = matricula + 4 ultimos digitos do telefone; troca obrigatoria no 1o login)"
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Seed Manaus concluido: "
                f"{Company.objects.count()} empresas, "
                f"{Person.objects.count()} pessoas, "
                f"{Address.objects.count()} enderecos, "
                f"{MeetingPoint.objects.count()} pontos, "
                f"{Route.objects.count()} rotas, "
                f"{RoutePassenger.objects.count()} passageiros de rota."
            )
        )

    def create_demo_manager(self, company):
        """Funcionario da 1a empresa com perfil MANAGER, para entrar no frontend."""
        person, _ = Person.objects.update_or_create(
            company=company,
            type=Person.PersonType.EMPLOYEE,
            employee_code="MNG001",
            defaults={"name": "Maria Gestora", "phone": "92990000001", "is_active": True},
        )
        account = person.access_account
        if account.role != AccessAccount.Role.MANAGER:
            account.role = AccessAccount.Role.MANAGER
            account.save(update_fields=["role", "updated_at"])
        return person

    def create_companies(self):
        companies = []
        for item in COMPANIES:
            company, _ = Company.objects.update_or_create(
                name=item["name"],
                defaults={"email_domain": item["email_domain"], "is_active": True},
            )
            Address.objects.get_or_create(
                company=company,
                person=None,
                street=item["street"],
                number=item["number"],
                city="Manaus",
                state="AM",
                zip_code=item["zip_code"],
                latitude=Decimal(item["lat"]),
                longitude=Decimal(item["lng"]),
            )
            companies.append(company)
        return companies

    def create_schedules(self, companies):
        for company in companies:
            for shift_time in [time(6, 30), time(17, 45)]:
                Schedule.objects.get_or_create(
                    company=company,
                    weekdays=["MON", "TUE", "WED", "THU", "FRI"],
                    time=shift_time,
                )

    def create_meeting_points(self, companies):
        points_by_company = {}
        for company_index, company in enumerate(companies):
            points_by_company[company.id] = []
            for offset in range(5):
                neighborhood = NEIGHBORHOODS[(company_index * 3 + offset) % len(NEIGHBORHOODS)]
                name = f"Ponto {neighborhood[0]} - {company.name}"
                address, _ = Address.objects.get_or_create(
                    company=company,
                    person=None,
                    street=neighborhood[1],
                    number=str(100 + company_index * 20 + offset),
                    city="Manaus",
                    state="AM",
                    zip_code=f"690{company_index}{offset}-000",
                    latitude=Decimal(neighborhood[2]),
                    longitude=Decimal(neighborhood[3]),
                )
                point, _ = MeetingPoint.objects.get_or_create(
                    company=company,
                    name=name,
                    defaults={"address": address},
                )
                if point.address_id != address.id:
                    point.address = address
                    point.save(update_fields=["address", "updated_at"])
                points_by_company[company.id].append(point)
        return points_by_company

    def create_people(self, companies, total, person_type):
        people = []
        prefix = "MND" if person_type == Person.PersonType.DRIVER else "MNE"
        for index in range(total):
            company = companies[index % len(companies)]
            code = f"{prefix}{index + 1:03d}"
            name = self.person_name(index)
            defaults = {
                "name": name,
                "phone": f"+5592{980000000 + index:09d}",
                "type": person_type,
                "is_active": True,
            }
            if person_type == Person.PersonType.DRIVER:
                lookup = {"company": company, "license_number": code}
                defaults["employee_code"] = None
            else:
                lookup = {"company": company, "employee_code": code}
                defaults["license_number"] = None

            person, _ = Person.objects.update_or_create(**lookup, defaults=defaults)
            self.create_person_address(person, index)
            people.append(person)
        return people

    def create_person_address(self, person, index):
        neighborhood = NEIGHBORHOODS[index % len(NEIGHBORHOODS)]
        Address.objects.get_or_create(
            company=None,
            person=person,
            street=neighborhood[1],
            number=str(50 + index),
            city="Manaus",
            state="AM",
            zip_code=f"690{index % 10}{index % 7}-010",
            latitude=Decimal(neighborhood[2]),
            longitude=Decimal(neighborhood[3]),
        )

    def create_vehicles(self, companies, drivers):
        vehicles = []
        for index, driver in enumerate(drivers):
            company = companies[index % len(companies)]
            vehicle, _ = Vehicle.objects.update_or_create(
                plate_number=f"MAN{1000 + index}",
                defaults={
                    "company": company,
                    "driver": driver,
                    "model": VEHICLE_MODELS[index % len(VEHICLE_MODELS)],
                    "capacity": 18 + (index % 4) * 6,
                    "is_active": True,
                },
            )
            vehicles.append(vehicle)
        return vehicles

    def create_routes(self, companies, vehicles, meeting_points):
        routes = []
        for index, vehicle in enumerate(vehicles):
            company = companies[index % len(companies)]
            route_name = f"Rota Manaus {index + 1:02d} - {company.name}"
            route, _ = Route.objects.update_or_create(
                company=company,
                name=route_name,
                defaults={"vehicle": vehicle, "is_active": True},
            )
            routes.append(route)
        return routes

    def create_route_stops(self, routes, meeting_points):
        stops_by_route = {}
        for route in routes:
            points = meeting_points[route.company_id]
            stops_by_route[route.id] = []
            for sequence, point in enumerate(points[:4]):
                stop, _ = RouteStop.objects.update_or_create(
                    route=route,
                    sequence=sequence,
                    defaults={"meeting_point": point, "is_active": True},
                )
                stops_by_route[route.id].append(stop)
        return stops_by_route

    def create_route_passengers(self, employees, routes, stops_by_route):
        statuses = [
            RoutePassenger.BoardingStatus.PENDING,
            RoutePassenger.BoardingStatus.CONFIRMED,
            RoutePassenger.BoardingStatus.ABSENT,
        ]
        for index, employee in enumerate(employees):
            available_routes = [route for route in routes if route.company_id == employee.company_id]
            route = available_routes[index % len(available_routes)]
            stops = stops_by_route[route.id]
            RoutePassenger.objects.update_or_create(
                person=employee,
                route=route,
                defaults={
                    "route_stop": stops[index % len(stops)],
                    "boarding_status": statuses[index % len(statuses)],
                    "is_active": True,
                },
            )

    def create_route_events(self, routes):
        event_data = [
            ("departure", "Saida da garagem no Distrito Industrial", "-3.129780", "-59.967430"),
            ("stop", "Parada realizada para embarque", "-3.091280", "-59.974560"),
            ("delay", "Transito intenso na Avenida Torquato Tapajos", "-3.060910", "-60.025900"),
            ("arrival", "Chegada confirmada no destino", "-3.091390", "-60.018890"),
        ]
        for route in routes:
            for event_type, description, lat, lng in event_data:
                RouteEvent.objects.get_or_create(
                    route=route,
                    event_type=event_type,
                    description=description,
                    latitude=Decimal(lat),
                    longitude=Decimal(lng),
                )

    def person_name(self, index):
        first = FIRST_NAMES[index % len(FIRST_NAMES)]
        last = LAST_NAMES[(index * 3) % len(LAST_NAMES)]
        return f"{first} {last}"
