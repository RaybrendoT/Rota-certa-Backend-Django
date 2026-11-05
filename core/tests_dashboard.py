"""Resumo do dia da tela inicial: GET dashboard/summary/."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings

from core.dashboard import dashboard_alerts
from core.models import Address, BoardingRecord, Route, RouteEvent, RoutePassenger, Schedule
from core.tests import RoleTestCase

URL = "/api/dashboard/summary/"
THURSDAY = date(2026, 11, 5)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(settings.ADMIN_TIME_ZONE))


class DashboardTestCase(RoleTestCase):
    """Rota A da base passa a ter turno às 06:00 (seg e qui)."""

    def setUp(self):
        super().setUp()
        self.morning = Schedule.objects.create(company=self.company, time=time(6, 0), weekdays=["MON", "THU"])
        self.route.schedule = self.morning
        self.route.save(update_fields=["schedule"])

    def event(self, route, event_type, when):
        ev = RouteEvent.objects.create(
            route=route, event_type=event_type, latitude=Decimal("-3.1"), longitude=Decimal("-60.0")
        )
        RouteEvent.objects.filter(pk=ev.pk).update(timestamp=when)

    def board(self, passenger, status, when):
        """Embarque registrado naquele dia (como core.boarding.record_boarding faria)."""
        RoutePassenger.objects.filter(pk=passenger.pk).update(boarding_status=status, updated_at=when)
        return BoardingRecord.objects.create(
            route_passenger=passenger, route_id=passenger.route_id, person_id=passenger.person_id,
            service_date=when.date(), status=status, created_at=when, updated_at=when,
        )


class DashboardSummaryTests(DashboardTestCase):
    def get(self, day=THURSDAY, person=None):
        r = self.client_for(person or self.manager_person).get(URL, {"date": day.isoformat()})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_rotas_do_dia_pelo_turno_e_rotas_avulsas(self):
        Route.objects.create(company=self.company, name="Avulsa hoje", service_date=THURSDAY)
        Route.objects.create(company=self.company, name="Avulsa amanhã", service_date=THURSDAY + timedelta(days=1))
        friday_only = Schedule.objects.create(company=self.company, time=time(14, 0), weekdays=["FRI"])
        Route.objects.create(company=self.company, name="Só sexta", schedule=friday_only)

        data = self.get()
        self.assertEqual(data["routes"]["total"], 2)
        self.assertEqual(data["routes"]["without_vehicle"], 1)
        self.assertEqual(self.get(THURSDAY + timedelta(days=1))["routes"]["total"], 2)  # Só sexta + avulsa

    def test_status_das_rotas_e_pontualidade(self):
        late_route = Route.objects.create(
            company=self.company, name="Rota B", schedule=self.morning, vehicle=self.route.vehicle
        )
        Route.objects.create(company=self.company, name="Rota C", schedule=self.morning, vehicle=self.route.vehicle)
        self.event(self.route, "departure", at(THURSDAY, 5, 0))
        self.event(self.route, "arrival", at(THURSDAY, 6, 5))  # dentro da tolerância
        self.event(late_route, "departure", at(THURSDAY, 5, 10))
        self.event(late_route, "delay", at(THURSDAY, 5, 40))
        self.event(late_route, "arrival", at(THURSDAY, 6, 30))
        self.event(self.route, "incident", at(THURSDAY - timedelta(days=1), 5, 30))  # outro dia: não conta

        data = self.get()
        self.assertEqual(data["routes"], {
            "total": 3, "not_started": 1, "in_progress": 0, "finished": 2, "without_vehicle": 0,
        })
        self.assertEqual(data["events"], {"delays": 1, "incidents": 0})
        self.assertEqual(data["punctuality"]["on_time"], 1)
        self.assertEqual(data["punctuality"]["late"], 1)
        self.assertEqual(data["punctuality"]["rate"], 0.5)

    def test_rota_em_andamento(self):
        self.event(self.route, "departure", at(THURSDAY, 5, 0))
        data = self.get()
        self.assertEqual(data["routes"]["in_progress"], 1)
        self.assertIsNone(data["punctuality"]["rate"])

    def test_embarques_contam_pelo_registro_do_dia(self):
        second = self.employee(self.company, "Beltrano", "E002")
        third = self.employee(self.company, "Ciclano", "E003")
        self.board(self.passenger, "confirmed", at(THURSDAY, 5, 20))
        absent = RoutePassenger.objects.create(person=second, route=self.route)
        self.board(absent, "absent", at(THURSDAY, 4, 0))
        old = RoutePassenger.objects.create(person=third, route=self.route)
        self.board(old, "confirmed", at(THURSDAY - timedelta(days=1), 5, 20))  # outro dia: pendente hoje

        data = self.get()
        self.assertEqual(data["passengers"], {"expected": 3, "confirmed": 1, "absent": 1, "pending": 1})
        self.assertEqual(data["occupancy"], {"seats": 10, "passengers": 3, "rate": 0.3})
        self.assertEqual(data["shifts"], [{
            "schedule_id": self.morning.id, "time": "06:00", "routes": 1, "in_progress": 0, "finished": 0,
            "passengers": 3, "confirmed": 1, "absent": 1, "seats": 10, "occupancy_rate": 0.3,
        }])

    def test_escopo_da_empresa_e_permissao(self):
        other_schedule = Schedule.objects.create(company=self.other_company, time=time(6, 0), weekdays=["THU"])
        self.other_route.schedule = other_schedule
        self.other_route.save(update_fields=["schedule"])
        self.assertEqual(self.get()["routes"]["total"], 1)

        for person in (self.employee_person, self.driver_person, self.gerente_person):
            self.assertEqual(self.client_for(person).get(URL).status_code, 403)

    def test_sem_data_usa_hoje_e_data_invalida_da_400(self):
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get(URL).status_code, 200)
        self.assertEqual(client.get(URL, {"date": "ontem"}).status_code, 400)


ALERTS_URL = "/api/dashboard/alerts/"


class DashboardAlertsTests(DashboardTestCase):
    def alerts(self, day=THURSDAY, now=None):
        data = dashboard_alerts(self.company, day, now=now)
        return data, {group["kind"]: group for group in data["groups"]}

    def test_incidentes_e_atrasos_registrados_no_dia(self):
        self.event(self.route, "delay", at(THURSDAY, 5, 40))
        self.event(self.route, "incident", at(THURSDAY, 5, 50))
        self.event(self.route, "incident", at(THURSDAY - timedelta(days=1), 5, 50))  # outro dia

        _, groups = self.alerts(now=at(THURSDAY, 5, 55))
        self.assertEqual(groups["incidents"]["count"], 1)
        self.assertEqual(groups["incidents"]["items"][0]["time"], "05:50")
        self.assertEqual(groups["incidents"]["items"][0]["route_name"], "Rota A")
        self.assertEqual(
            [(i["route_name"], i["reason"], i["time"]) for i in groups["delays"]["items"]],
            [("Rota A", "reported", "05:40")],
        )

    def test_rota_que_passou_do_horario_sem_chegar_so_conta_hoje(self):
        on_time = Route.objects.create(
            company=self.company, name="Rota B", schedule=self.morning, vehicle=self.route.vehicle
        )
        self.event(on_time, "arrival", at(THURSDAY, 6, 0))

        _, before = self.alerts(now=at(THURSDAY, 6, 5))  # ainda dentro da tolerância
        self.assertEqual(before["delays"]["count"], 0)

        _, after = self.alerts(now=at(THURSDAY, 6, 30))
        self.assertEqual(
            [(i["route_name"], i["reason"]) for i in after["delays"]["items"]], [("Rota A", "overdue")]
        )

        _, other_day = self.alerts(now=at(THURSDAY + timedelta(days=4), 9, 0))  # olhando o dia passado
        self.assertEqual(other_day["delays"]["count"], 0)

    def test_rotas_sem_veiculo_ou_sem_motorista(self):
        Route.objects.create(company=self.company, name="Sem veículo", schedule=self.morning)
        vehicle = self.route.vehicle
        vehicle.driver = None
        vehicle.save(update_fields=["driver"])

        _, groups = self.alerts(now=at(THURSDAY, 4, 0))
        self.assertEqual(
            sorted((i["route_name"], i["reason"]) for i in groups["unassigned"]["items"]),
            [("Rota A", "no_driver"), ("Sem veículo", "no_vehicle")],
        )

    def test_enderecos_aguardando_aprovacao(self):
        common = dict(street="Rua A", number="1", city="Manaus", state="AM", zip_code="69000-000",
                      latitude=Decimal("-3.1"), longitude=Decimal("-60.0"))
        Address.objects.create(person=self.employee_person, source=Address.Source.GPS, review_status="pending", **common)
        Address.objects.create(person=self.employee_person, source=Address.Source.TYPED, review_status="approved", **common)
        Address.objects.create(person=self.other_driver_person, source=Address.Source.GPS, review_status="pending", **common)

        data, groups = self.alerts(now=at(THURSDAY, 4, 0))
        self.assertEqual(groups["address_reviews"]["count"], 1)
        self.assertEqual(groups["address_reviews"]["items"][0]["person_name"], "Fulano")
        self.assertEqual(data["total"], 1)

    def test_grupo_traz_todos_os_itens(self):
        for n in range(7):
            Route.objects.create(company=self.company, name=f"Extra {n}", service_date=THURSDAY)
        _, groups = self.alerts(now=at(THURSDAY, 4, 0))
        self.assertEqual(groups["unassigned"]["count"], 7)
        self.assertEqual(len(groups["unassigned"]["items"]), 7)

    def test_endpoint_e_permissao(self):
        r = self.client_for(self.manager_person).get(ALERTS_URL, {"date": THURSDAY.isoformat()})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([g["kind"] for g in r.json()["groups"]], ["incidents", "delays", "unassigned", "address_reviews"])
        self.assertEqual(self.client_for(self.driver_person).get(ALERTS_URL).status_code, 403)
        self.assertEqual(self.client_for(self.manager_person).get(ALERTS_URL, {"date": "x"}).status_code, 400)


FEED_URL = "/api/dashboard/feed/"


class DashboardFeedTests(DashboardTestCase):
    def test_eventos_e_embarques_do_dia_do_mais_novo_ao_mais_antigo(self):
        self.event(self.route, "departure", at(THURSDAY, 5, 0))
        self.event(self.route, "incident", at(THURSDAY, 5, 30))
        self.event(self.route, "arrival", at(THURSDAY - timedelta(days=1), 6, 0))  # outro dia
        self.event(self.other_route, "departure", at(THURSDAY, 5, 10))  # outra empresa
        self.board(self.passenger, "confirmed", at(THURSDAY, 5, 20))
        self.board(self.other_passenger, "absent", at(THURSDAY, 5, 25))  # outra empresa

        r = self.client_for(self.manager_person).get(FEED_URL, {"date": THURSDAY.isoformat()})
        self.assertEqual(r.status_code, 200, r.content)
        items = r.json()["items"]
        self.assertEqual(
            [(i["type"], i.get("event_type") or i.get("status")) for i in items],
            [("route_event", "incident"), ("boarding_status", "confirmed"), ("route_event", "departure")],
        )
        self.assertEqual(items[1]["person_name"], "Fulano")
        self.assertEqual(items[0]["route_name"], "Rota A")

    def test_embarque_pendente_nao_entra(self):
        self.board(self.passenger, "pending", at(THURSDAY, 5, 20))
        r = self.client_for(self.manager_person).get(FEED_URL, {"date": THURSDAY.isoformat()})
        self.assertEqual(r.json()["items"], [])

    def test_limite_e_permissao(self):
        for minute in range(45):
            self.event(self.route, "stop", at(THURSDAY, 5, minute))
        r = self.client_for(self.manager_person).get(FEED_URL, {"date": THURSDAY.isoformat()})
        self.assertEqual(len(r.json()["items"]), 40)
        self.assertEqual(self.client_for(self.driver_person).get(FEED_URL).status_code, 403)


MAP_URL = "/api/dashboard/map/"


class DashboardMapTests(DashboardTestCase):
    def get(self, day=THURSDAY):
        r = self.client_for(self.manager_person).get(MAP_URL, {"date": day.isoformat()})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_rota_com_paradas_destino_e_posicao_do_dia(self):
        from core.models import MeetingPoint, RouteStop, VehicleLocation
        from core.tests_realtime import make_stop

        make_stop(self.route, "Ponto 2", 1, Decimal("-3.10"), Decimal("-60.01"))
        make_stop(self.route, "Ponto 1", 0, Decimal("-3.11"), Decimal("-60.02"))
        Address.objects.create(
            company=self.company, street="Av. Fábrica", number="1", city="Manaus", state="AM",
            zip_code="69000-000", latitude=Decimal("-3.12"), longitude=Decimal("-59.95"),
        )
        location = VehicleLocation.objects.create(
            route=self.route, latitude=Decimal("-3.105"), longitude=Decimal("-60.015"), heading=45.0,
            recorded_at=at(THURSDAY, 5, 30),
        )
        self.event(self.route, "departure", at(THURSDAY, 5, 0))

        data = self.get()
        self.assertEqual(data["destination"], {"name": "Acme", "latitude": -3.12, "longitude": -59.95})
        [route] = data["routes"]
        self.assertEqual((route["name"], route["time"], route["state"]), ("Rota A", "06:00", "in_progress"))
        self.assertEqual(route["vehicle"]["plate"], "AAA-0001")
        self.assertEqual(route["driver_name"], "Motorista Um")
        self.assertEqual(route["passengers"], 1)
        self.assertEqual([s["name"] for s in route["stops"]], ["Ponto 1", "Ponto 2"])
        self.assertEqual(route["location"]["heading"], 45.0)
        self.assertEqual(route["location"]["latitude"], -3.105)
        self.assertTrue(MeetingPoint.objects.exists() and RouteStop.objects.count() == 2)
        self.assertEqual(location.route_id, route["id"])

    def test_posicao_de_outro_dia_nao_aparece_e_rota_atrasada_marcada(self):
        from core.models import VehicleLocation

        VehicleLocation.objects.create(
            route=self.route, latitude=Decimal("-3.1"), longitude=Decimal("-60.0"),
            recorded_at=at(THURSDAY - timedelta(days=1), 5, 30),
        )
        self.event(self.route, "delay", at(THURSDAY, 5, 40))
        [route] = self.get()["routes"]
        self.assertIsNone(route["location"])
        self.assertTrue(route["delayed"])
        self.assertEqual(route["state"], "not_started")

    def test_rota_finalizada_e_permissao(self):
        self.event(self.route, "departure", at(THURSDAY, 5, 0))
        self.event(self.route, "arrival", at(THURSDAY, 6, 0))
        data = self.get()
        self.assertEqual(data["routes"][0]["state"], "finished")
        self.assertIsNone(data["destination"])
        self.assertEqual(self.client_for(self.driver_person).get(MAP_URL).status_code, 403)
