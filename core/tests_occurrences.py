"""Ocorrências: detecção automática de atrasos, registro pelo gestor, status e dashboard."""
from datetime import datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache

from core import occurrences
from core.dates import local_today
from core.models import MeetingPoint, Notification, RouteEvent, RouteStop, Schedule
from core.tests import RoleTestCase
from core.tests_planning import address

DEPOT = (-3.1135, -59.9675)
POINT = (-3.0300, -59.9800)  # ~9 km da empresa: saída prevista bem antes das 07:00
ALL_DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


class OccurrenceBase(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        address(*DEPOT, company=cls.company)
        point = MeetingPoint.objects.create(company=cls.company, name="Norte – Rua A", address=address(*POINT, company=cls.company))
        cls.schedule = Schedule.objects.create(company=cls.company, time=time(7, 0), weekdays=ALL_DAYS)
        cls.route.schedule = cls.schedule
        cls.route.save(update_fields=["schedule", "updated_at"])
        stop = RouteStop.objects.create(route=cls.route, meeting_point=point, sequence=0)
        cls.passenger.route_stop = stop
        cls.passenger.save(update_fields=["route_stop", "updated_at"])

    def setUp(self):
        super().setUp()
        cache.clear()
        self.tz = ZoneInfo(settings.ADMIN_TIME_ZONE)
        self.today = local_today()

    def at(self, hour, minute):
        return datetime.combine(self.today, time(hour, minute), tzinfo=self.tz)

    def detect(self, hour, minute):
        return occurrences.detect(self.company, now=self.at(hour, minute), force=True)

    def register(self, event_type, hour, minute):
        event = RouteEvent.objects.create(route=self.route, event_type=event_type, latitude=POINT[0], longitude=POINT[1])
        RouteEvent.objects.filter(pk=event.pk).update(timestamp=self.at(hour, minute))
        return event

    def system(self, code):
        return RouteEvent.objects.filter(route=self.route, source="system", auto_code=code)


class DetectionTests(OccurrenceBase):
    def test_nada_antes_da_saida_prevista(self):
        self.assertEqual(self.detect(5, 0), 0)
        self.assertFalse(RouteEvent.objects.filter(route=self.route).exists())

    def test_rota_nao_iniciada_vira_ocorrencia_do_sistema_sem_push(self):
        self.assertEqual(self.detect(6, 55), 1)
        [event] = self.system("not_started")
        self.assertEqual((event.event_type, event.status, event.category), ("delay", "open", "schedule"))
        self.assertIn("não iniciada", event.description)
        self.assertGreater(event.delay_minutes, 10)
        self.assertFalse(Notification.objects.filter(recipient=self.employee_person, route=self.route).exists())
        self.assertEqual(self.detect(6, 58), 0)  # não duplica

    def test_partida_resolve_a_nao_iniciada(self):
        self.detect(6, 55)
        self.register("departure", 6, 56)
        self.detect(6, 57)
        event = self.system("not_started").get()
        self.assertEqual(event.status, "resolved")
        self.assertIn("06:56", event.resolution)

    def test_passou_do_horario_e_chegada_resolve(self):
        self.register("departure", 6, 30)
        self.detect(7, 15)
        overdue = self.system("overdue").get()
        self.assertEqual(overdue.status, "open")
        self.register("arrival", 7, 25)
        self.detect(7, 26)
        overdue.refresh_from_db()
        self.assertEqual(overdue.status, "resolved")
        self.assertIn("25 min", overdue.resolution)
        self.assertFalse(self.system("late_arrival").exists())

    def test_chegada_atrasada_sem_deteccao_antes(self):
        self.register("departure", 6, 30)
        self.register("arrival", 7, 20)
        self.detect(7, 30)
        event = self.system("late_arrival").get()
        self.assertEqual(event.delay_minutes, 20)

    def test_chegada_no_horario_nao_gera_nada(self):
        self.register("departure", 6, 30)
        self.register("arrival", 7, 5)
        self.detect(9, 0)
        self.assertFalse(RouteEvent.objects.filter(source="system").exists())


class ManagerFlowTests(OccurrenceBase):
    def setUp(self):
        super().setUp()
        self.client_manager = self.client_for(self.manager_person)

    def create(self, **extra):
        payload = {"route": self.route.pk, "event_type": "incident", "description": "Pneu furado", "category": "mechanical", **extra}
        return self.client_manager.post("/api/route-events/", payload, format="json")

    def test_gestor_registra_sem_posicao_e_sem_avisar_passageiros(self):
        r = self.create()
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual((body["source"], body["status"], body["reported_by_name"]), ("manager", "open", "Maria Gestora"))
        self.assertIsNone(body["latitude"])
        self.assertFalse(Notification.objects.filter(recipient=self.employee_person, route=self.route).exists())

    def test_gestor_pode_avisar_passageiros(self):
        self.create(notify_passengers=True)
        self.assertTrue(Notification.objects.filter(recipient=self.employee_person, kind="route_incident").exists())

    def test_motorista_continua_avisando_e_fica_como_origem(self):
        client = self.client_for(self.driver_person)
        r = client.post("/api/route-events/", {
            "route": self.route.pk, "event_type": "delay", "description": "Trânsito", "latitude": "-3.1", "longitude": "-60.0",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["source"], "driver")
        self.assertTrue(Notification.objects.filter(recipient=self.employee_person, kind="route_delay").exists())

    def test_app_do_motorista_nao_recebe_ocorrencia_sem_posicao(self):
        self.create()  # gestor: sem latitude/longitude
        self.register("departure", 6, 30)
        types = [e["event_type"] for e in self.client_for(self.driver_person).get("/api/route-events/").json()["results"]]
        self.assertEqual(types, ["departure"])

    def test_assumir_resolver_e_reabrir(self):
        event_id = self.create().json()["id"]
        base = f"/api/route-events/{event_id}"
        self.assertEqual(self.client_manager.post(f"{base}/start/").json()["status"], "in_progress")
        self.assertEqual(self.client_manager.post(f"{base}/resolve/", {}, format="json").status_code, 400)
        resolved = self.client_manager.post(f"{base}/resolve/", {"resolution": "Ônibus reserva enviado"}, format="json").json()
        self.assertEqual((resolved["status"], resolved["resolved_by_name"]), ("resolved", "Maria Gestora"))
        self.assertEqual(self.client_manager.post(f"{base}/reopen/").json()["status"], "open")

    def test_partida_nao_tem_status_e_motorista_nao_resolve(self):
        departure = self.register("departure", 6, 30)
        self.assertEqual(self.client_manager.post(f"/api/route-events/{departure.pk}/resolve/", {"resolution": "x"}, format="json").status_code, 400)
        event_id = self.create().json()["id"]
        driver = self.client_for(self.driver_person)
        self.assertEqual(driver.post(f"/api/route-events/{event_id}/resolve/", {"resolution": "x"}, format="json").status_code, 403)

    @mock.patch("core.viewsets.occurrences.detect")  # sem os atrasos do sistema do horário real
    def test_filtros_da_tela_de_ocorrencias(self, _detect):
        self.register("departure", 6, 30)
        open_id = self.create().json()["id"]
        done_id = self.create(event_type="delay", description="Chuva", category="weather").json()["id"]
        self.client_manager.post(f"/api/route-events/{done_id}/resolve/", {"resolution": "ok"}, format="json")

        def ids(query):
            return {e["id"] for e in self.client_manager.get(f"/api/route-events/?page_size=100&{query}").json()["results"]}

        self.assertEqual(ids("occurrence=true"), {open_id, done_id})
        self.assertEqual(ids("occurrence=true&status=open,in_progress"), {open_id})
        self.assertEqual(ids("occurrence=true&source=manager&category=mechanical"), {open_id})
        self.assertEqual(ids(f"occurrence=true&date_from={self.today}&search=chuva"), {done_id})


class DashboardTests(OccurrenceBase):
    def test_card_conta_atrasos_do_sistema_e_alerta_so_os_abertos(self):
        self.detect(7, 30)  # não iniciada + passou do horário
        client = self.client_for(self.manager_person)
        summary = client.get("/api/dashboard/summary/").json()
        self.assertEqual(summary["events"]["delays"], 2)
        self.assertEqual(summary["events"]["open"], 2)

        delays = next(g for g in client.get("/api/dashboard/alerts/").json()["groups"] if g["kind"] == "delays")
        self.assertEqual(delays["count"], 1)  # uma linha por rota
        self.assertIn(delays["items"][0]["reason"], ("not_started", "overdue"))

        for event in RouteEvent.objects.filter(source="system"):
            occurrences.resolve(event, self.manager_person, "Motorista avisou que a rota foi cancelada")
        delays = next(g for g in client.get("/api/dashboard/alerts/").json()["groups"] if g["kind"] == "delays")
        self.assertEqual(delays["count"], 0)  # resolvida não volta como "passou do horário"
