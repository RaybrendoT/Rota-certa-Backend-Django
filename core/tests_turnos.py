"""Turno da rota (Route.schedule) nas APIs e o seed da empresa simulada de Manaus."""
from datetime import time
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from core.management.commands.seed_manaus_empresa import COMPANY_NAME, MAX_LOAD, MAX_STOPS, distribute
from core.models import Company, Person, Route, RoutePassenger, Schedule
from core.seed_data.manaus_bairros import BAIRROS
from core.tests import FAST_HASHERS, RoleTestCase
from core.tests_realtime import make_stop


class RouteScheduleApiTests(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.morning = Schedule.objects.create(company=cls.company, weekdays=["MON"], time=time(6, 0))
        cls.afternoon = Schedule.objects.create(company=cls.company, weekdays=["MON"], time=time(14, 0))
        cls.foreign = Schedule.objects.create(company=cls.other_company, weekdays=["MON"], time=time(7, 0))
        cls.route.schedule = cls.afternoon
        cls.route.save()
        # Segunda rota do mesmo motorista (mesmo veículo), no turno da manhã.
        cls.early = Route.objects.create(
            company=cls.company, name="Rota Cedo", vehicle=cls.route.vehicle, schedule=cls.morning
        )
        cls.stop = make_stop(cls.route)
        cls.passenger.route_stop = cls.stop
        cls.passenger.save()

    def test_motorista_ve_rotas_na_ordem_dos_turnos(self):
        routes = self.client_for(self.driver_person).get("/api/routes/my-routes/").json()
        self.assertEqual([r["name"] for r in routes], ["Rota Cedo", "Rota A"])
        self.assertEqual(routes[0]["schedule"], {"id": self.morning.pk, "time": "06:00", "weekdays": ["MON"]})

    def test_rota_sem_turno_vem_no_fim(self):
        Route.objects.create(company=self.company, name="Rota Sem Turno", vehicle=self.route.vehicle)
        routes = self.client_for(self.driver_person).get("/api/routes/my-routes/").json()
        self.assertEqual(routes[-1]["name"], "Rota Sem Turno")
        self.assertIsNone(routes[-1]["schedule"])

    def test_colaborador_ve_o_horario_da_propria_rota(self):
        [item] = self.client_for(self.employee_person).get("/api/me/routes/").json()
        self.assertEqual(item["route"]["schedule"]["time"], "14:00")
        self.assertEqual(item["schedules"], [{"weekdays": ["MON"], "time": "14:00"}])

    def test_gestor_atribui_turno(self):
        client = self.client_for(self.manager_person)
        r = client.patch(f"/api/routes/{self.route.pk}/", {"schedule": self.morning.pk}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["schedule"], self.morning.pk)

    def test_turno_de_outra_empresa_e_recusado(self):
        client = self.client_for(self.manager_person)
        r = client.patch(f"/api/routes/{self.route.pk}/", {"schedule": self.foreign.pk}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("schedule", r.json())


class SeedHelpersTests(TestCase):
    def test_distribute_soma_exatamente(self):
        self.assertEqual(distribute(1000, [0.45, 0.20, 0.35]), [450, 200, 350])
        counts = distribute(1000, [b.weight for b in BAIRROS])
        self.assertEqual(sum(counts), 1000)
        self.assertTrue(all(c > 0 for c in counts))

    def test_bairros_tem_pontos_de_parada(self):
        self.assertTrue(all(b.stops for b in BAIRROS))


@FAST_HASHERS
class SeedEmpresaTests(TestCase):
    def test_seed_monta_rotas_por_turno_dentro_da_lotacao(self):
        out = StringIO()
        call_command("seed_manaus_empresa", employees=300, stdout=out)
        company = Company.objects.get(name=COMPANY_NAME)
        employees = Person.objects.filter(company=company, type=Person.PersonType.EMPLOYEE, employee_code__startswith="NC")
        self.assertEqual(employees.count(), 300)
        # Cada colaborador está em exatamente uma rota, com parada definida.
        passengers = RoutePassenger.objects.filter(route__company=company)
        self.assertEqual(passengers.count(), 300)
        self.assertFalse(passengers.filter(route_stop__isnull=True).exists())
        self.assertEqual(passengers.values("person").distinct().count(), 300)

        routes = Route.objects.filter(company=company).select_related("schedule", "vehicle__driver")
        self.assertEqual({r.schedule.time for r in routes}, {time(6, 0), time(8, 0), time(14, 0)})
        for route in routes:
            self.assertLessEqual(route.route_passengers.count(), MAX_LOAD, route.name)
            # Parada do passageiro pertence à rota dele.
            self.assertFalse(route.route_passengers.exclude(route_stop__route=route).exists())

        for route in routes:
            self.assertLessEqual(route.stops.count(), MAX_STOPS, route.name)

        # Todo motorista atende pelo menos dois turnos; ninguém tem duas rotas no mesmo turno.
        by_driver = {}
        for route in routes:
            by_driver.setdefault(route.vehicle.driver_id, []).append(route.schedule.time)
        self.assertTrue(all(len(set(times)) >= 2 for times in by_driver.values()), by_driver)
        self.assertTrue(all(len(times) == len(set(times)) for times in by_driver.values()))

        # Contas criadas e rodar de novo não duplica.
        self.assertTrue(all(hasattr(p, "access_account") for p in employees))
        call_command("seed_manaus_empresa", employees=300, stdout=StringIO())
        self.assertEqual(employees.count(), 300)
        # Gestor da empresa para o painel e o assistente.
        self.assertEqual(Person.objects.get(employee_code="GESTOR01").access_account.role, "MANAGER")
        self.assertEqual(RoutePassenger.objects.filter(route__company=company).count(), 300)
