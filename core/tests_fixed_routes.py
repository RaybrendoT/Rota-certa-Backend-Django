"""Rotas fixas de um turno cobrindo todos os colaboradores (core/fixed_routes.py e o comando)."""
from datetime import time
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

from core import fixed_routes
from core.models import AccessAccount, Notification, Route, RoutePassenger, Schedule, Vehicle
from core.tests_planning import POINTS, PlanningBase, address

MON_FRI = ["MON", "TUE", "WED", "THU", "FRI"]


class FixedRoutesBase(PlanningBase):
    """PlanningBase: 16 colaboradores com endereço (6 Norte, 5 Sul, 4 Oeste e o José), dois ônibus de 10
    lugares livres e uma van de 6 cujo motorista tem rota fixa às 06:00 todos os dias."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.schedule = Schedule.objects.create(company=cls.company, time=time(9, 0), weekdays=MON_FRI)
        cls.with_home = cls.riders + [cls.jose]

    def run_command(self, *args):
        out = StringIO()
        call_command("criar_rotas_fixas", "--empresa", "Acme", *args, stdout=out)
        return out.getvalue()


class PlanTests(FixedRoutesBase):
    def test_cobre_todos_com_endereco_sem_repetir_e_dentro_da_lotacao(self):
        planned = fixed_routes.plan(self.company, self.schedule, slack_percent=0)
        people = [p.id for route in planned.routes for _, riders in route.stops for p in riders]
        self.assertEqual(sorted(people), sorted(p.id for p in self.with_home))
        for route in planned.routes:
            self.assertLessEqual(route.load, route.vehicle.capacity)
            self.assertLessEqual(len(route.stops), fixed_routes.MAX_STOPS)
        # Sem endereço fica de fora e aparece no relatório; gestora e gerente não são passageiros.
        self.assertIn("Joana Silva", {p.name for p in planned.without_home})
        self.assertNotIn(self.manager_person.id, people)

    def test_motorista_com_rota_perto_do_horario_fica_de_fora(self):
        early = Schedule.objects.create(company=self.company, time=time(7, 0), weekdays=MON_FRI)
        planned = fixed_routes.plan(self.company, early, slack_percent=0)
        self.assertEqual([v.plate_number for v, _ in planned.busy_drivers], ["ACM-0011"])
        self.assertNotIn("ACM-0011", {route.vehicle.plate_number for route in planned.routes})

    def test_nao_cabe_avisa_com_os_numeros(self):
        Vehicle.objects.filter(plate_number__in=["ACM-0010", "ACM-0011"]).delete()  # sobra um ônibus de 10
        with self.assertRaisesMessage(fixed_routes.FixedRoutesError, "Não cabe: 16 colaboradores para 10 lugares"):
            fixed_routes.plan(self.company, self.schedule, slack_percent=0)

    def test_ponto_com_mais_gente_que_o_onibus_e_dividido(self):
        lat, lng = POINTS["Norte"]
        for index in range(8):  # Norte passa a ter 14 pessoas; o maior ônibus leva 10
            person = self.employee(self.company, f"Extra Norte {index}", f"XN{index:02d}")
            address(lat, lng + 0.0005, person=person)
        Vehicle.objects.create(
            company=self.company, driver=self.driver(self.company, "Motorista Três", "D012"),
            plate_number="ACM-0012", model="Ônibus", capacity=10,
        )
        planned = fixed_routes.plan(self.company, self.schedule, slack_percent=0)
        north_routes = [
            route for route in planned.routes if any(point.id == self.points["Norte"].id for point, _ in route.stops)
        ]
        self.assertEqual(len(north_routes), 2)
        self.assertEqual(planned.passengers, 24)
        self.assertTrue(all(route.load <= 10 for route in planned.routes))

    def test_gestao_entra_so_quando_pedido(self):
        address(*POINTS["Sul"], person=self.manager_person)
        default = fixed_routes.plan(self.company, self.schedule, slack_percent=0)
        with_managers = fixed_routes.plan(self.company, self.schedule, slack_percent=0, include_managers=True)
        self.assertEqual(with_managers.passengers, default.passengers + 1)
        self.assertEqual(self.manager.role, AccessAccount.Role.MANAGER)


class CommandTests(FixedRoutesBase):
    def fixed_routes_of_shift(self):
        return Route.objects.filter(company=self.company, schedule=self.schedule, service_date__isnull=True)

    def test_dry_run_nao_grava(self):
        output = self.run_command("--chegada", "09:00", "--folga", "0", "--dry-run")
        self.assertIn("nada foi gravado", output)
        self.assertFalse(self.fixed_routes_of_shift().exists())

    def test_cria_rotas_paradas_e_passageiros_e_nao_duplica(self):
        output = self.run_command("--chegada", "09:00", "--folga", "0")
        routes = list(self.fixed_routes_of_shift())
        self.assertGreaterEqual(len(routes), 2)
        self.assertIn(f"{len(routes)} rotas fixas criadas", output)
        self.assertTrue(all(route.name.startswith("Fixa 09:00 R") and route.vehicle_id for route in routes))
        rides = RoutePassenger.objects.filter(route__in=routes)
        self.assertEqual(rides.count(), 16)
        self.assertFalse(rides.filter(route_stop__isnull=True).exists())
        self.assertFalse(Notification.objects.filter(route__in=routes).exists())  # sem --avisar

        again = self.run_command("--chegada", "09:00", "--folga", "0")
        self.assertIn("Nada a criar", again)
        self.assertEqual(self.fixed_routes_of_shift().count(), len(routes))

    def test_recriar_e_avisar(self):
        self.run_command("--chegada", "09:00", "--folga", "0")
        first = set(self.fixed_routes_of_shift().values_list("id", flat=True))
        self.run_command("--chegada", "09:00", "--folga", "0", "--recriar", "--avisar")
        routes = self.fixed_routes_of_shift()
        self.assertFalse(first & set(routes.values_list("id", flat=True)))
        self.assertEqual(RoutePassenger.objects.filter(route__in=routes).count(), 16)
        # 16 passageiros + um aviso por motorista
        self.assertEqual(Notification.objects.filter(route__in=routes).count(), 16 + routes.count())

    def test_cria_o_turno_quando_nao_existe(self):
        self.run_command("--chegada", "10:30", "--dias", "SAT", "--folga", "0")
        schedule = Schedule.objects.get(company=self.company, time=time(10, 30))
        self.assertEqual(schedule.weekdays, ["SAT"])
        self.assertTrue(Route.objects.filter(schedule=schedule).exists())

    def test_erros_claros(self):
        with self.assertRaisesMessage(CommandError, "Empresa não encontrada"):
            call_command("criar_rotas_fixas", "--empresa", "Inexistente", "--chegada", "09:00")
        with self.assertRaisesMessage(CommandError, "HH:MM"):
            self.run_command("--chegada", "9h")
        with self.assertRaisesMessage(CommandError, "add_manaus_meeting_points"):
            call_command("criar_rotas_fixas", "--empresa", "Beta", "--chegada", "09:00", stdout=StringIO())
