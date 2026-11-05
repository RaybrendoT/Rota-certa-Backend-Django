"""Resumo do dia da tela inicial: GET dashboard/summary/."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings

from core.models import Route, RouteEvent, RoutePassenger, Schedule
from core.tests import RoleTestCase

URL = "/api/dashboard/summary/"
THURSDAY = date(2026, 11, 5)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(settings.ADMIN_TIME_ZONE))


class DashboardSummaryTests(RoleTestCase):
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

    def test_embarques_contam_so_quando_alterados_no_dia(self):
        second = self.employee(self.company, "Beltrano", "E002")
        third = self.employee(self.company, "Ciclano", "E003")
        RoutePassenger.objects.filter(pk=self.passenger.pk).update(
            boarding_status="confirmed", updated_at=at(THURSDAY, 5, 20)
        )
        absent = RoutePassenger.objects.create(person=second, route=self.route)
        RoutePassenger.objects.filter(pk=absent.pk).update(boarding_status="absent", updated_at=at(THURSDAY, 4, 0))
        old = RoutePassenger.objects.create(person=third, route=self.route)
        RoutePassenger.objects.filter(pk=old.pk).update(
            boarding_status="confirmed", updated_at=at(THURSDAY - timedelta(days=1), 5, 20)
        )

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
