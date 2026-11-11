"""Horários: lista com inativos sob demanda e filtros por status, dia da semana e período."""
from datetime import time

from core.models import Schedule
from core.tests import RoleTestCase

WEEK = ["MON", "TUE", "WED", "THU", "FRI"]


class ScheduleFilterTests(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.morning = Schedule.objects.create(company=cls.company, time=time(6, 30), weekdays=WEEK)
        cls.afternoon = Schedule.objects.create(company=cls.company, time=time(13, 0), weekdays=WEEK + ["SAT"])
        cls.night_off = Schedule.objects.create(company=cls.company, time=time(18, 0), weekdays=["TUE"], is_active=False)
        cls.foreign_off = Schedule.objects.create(company=cls.other_company, time=time(7, 0), weekdays=WEEK, is_active=False)

    def ids(self, query=""):
        response = self.client_for(self.manager_person).get(f"/api/schedules/?page_size=100&{query}")
        self.assertEqual(response.status_code, 200, response.content)
        return [s["id"] for s in response.json()["results"]]

    def test_padrao_continua_so_com_ativos_e_em_ordem_de_horario(self):
        self.assertEqual(self.ids(), [self.morning.id, self.afternoon.id])

    def test_status_inativo_e_todos(self):
        self.assertEqual(self.ids("status=inactive"), [self.night_off.id])
        self.assertEqual(self.ids("status=all"), [self.morning.id, self.afternoon.id, self.night_off.id])

    def test_dia_da_semana(self):
        self.assertEqual(self.ids("weekday=SAT"), [self.afternoon.id])
        self.assertEqual(self.ids("weekday=TUE&status=all"), [self.morning.id, self.afternoon.id, self.night_off.id])
        self.assertEqual(self.ids("weekday=SUN"), [])

    def test_periodo(self):
        self.assertEqual(self.ids("period=morning"), [self.morning.id])
        self.assertEqual(self.ids("period=afternoon"), [self.afternoon.id])
        self.assertEqual(self.ids("period=night&status=all"), [self.night_off.id])

    def test_inativo_abre_e_pode_ser_reativado(self):
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get(f"/api/schedules/{self.night_off.pk}/").status_code, 200)
        response = client.patch(f"/api/schedules/{self.night_off.pk}/", {"is_active": True}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(self.night_off.id, self.ids())

    def test_nao_ve_inativos_de_outra_empresa(self):
        self.assertNotIn(self.foreign_off.id, self.ids("status=all"))
        self.assertEqual(self.client_for(self.manager_person).get(f"/api/schedules/{self.foreign_off.pk}/").status_code, 404)
