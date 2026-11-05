"""Aprovação pelo gestor dos endereços enviados pelo app, com inclusão em rota fixa."""
from datetime import time
from decimal import Decimal

from core.models import (
    AccessAccount,
    Address,
    MeetingPoint,
    Notification,
    PlanningConversation,
    Route,
    RoutePassenger,
    RouteStop,
    Schedule,
    Vehicle,
)
from core.planning.planner import Planner
from core.tests import RoleTestCase

DEPOT = (-3.1135, -59.9675)
CENTRO = (-3.1300, -60.0200)
NORTE = (-3.0300, -59.9800)
NEW_HOME = (-3.1305, -60.0205)  # ~80 m do ponto Centro
OLD_HOME = (-3.0310, -59.9810)  # perto do ponto Norte


def address(lat, lng, **fields):
    return Address.objects.create(
        street="Rua Teste", number="1", city="Manaus", state="AM", zip_code="69000-000",
        latitude=Decimal(f"{lat:.6f}"), longitude=Decimal(f"{lng:.6f}"), **fields,
    )


class AddressReviewTests(RoleTestCase):
    URL = "/api/address-reviews/"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        address(*DEPOT, company=cls.company)
        centro = MeetingPoint.objects.create(company=cls.company, name="Centro – Rua A", address=address(*CENTRO, company=cls.company))
        norte = MeetingPoint.objects.create(company=cls.company, name="Norte – Rua B", address=address(*NORTE, company=cls.company))
        morning = Schedule.objects.create(company=cls.company, time=time(6, 0), weekdays=["MON", "TUE", "WED", "THU", "FRI"])

        def fixed_route(name, plate, capacity, points):
            vehicle = Vehicle.objects.create(company=cls.company, plate_number=plate, model="Ônibus", capacity=capacity)
            route = Route.objects.create(company=cls.company, name=name, vehicle=vehicle, schedule=morning)
            stops = [RouteStop.objects.create(route=route, meeting_point=p, sequence=i) for i, p in enumerate(points)]
            return route, stops

        cls.centro_route, _ = fixed_route("Linha Centro", "CEN-0001", 10, [centro])
        cls.mixed_route, (cls.mixed_norte, cls.mixed_centro) = fixed_route("Linha Mista", "MIX-0001", 10, [norte, centro])
        cls.full_route, _ = fixed_route("Linha Lotada", "FUL-0001", 1, [centro])
        RoutePassenger.objects.create(person=cls.manager_person, route=cls.full_route)

    def setUp(self):
        super().setUp()
        self.old = address(*OLD_HOME, person=self.employee_person)
        self.ride = RoutePassenger.objects.create(person=self.employee_person, route=self.mixed_route, route_stop=self.mixed_norte)
        self.pending = address(
            *NEW_HOME, person=self.employee_person, source=Address.Source.GPS, precision="gps", accuracy_m=12,
            is_active=False, review_status=Address.Review.PENDING, created_by=self.employee_person,
        )
        self.manager_api = self.client_for(self.manager_person)

    def test_lista_com_antes_depois_rotas_e_sugestoes(self):
        [item] = self.manager_api.get(self.URL).json()
        self.assertEqual(item["id"], self.pending.id)
        self.assertEqual(item["person"]["name"], "Fulano")
        self.assertEqual(item["previous_address"]["id"], self.old.id)
        self.assertGreater(item["moved_m"], 10_000)
        self.assertEqual(item["nearest_point"]["name"], "Centro – Rua A")
        self.assertIn("Linha Mista", [r["name"] for r in item["current_routes"]])
        names = [s["name"] for s in item["suggestions"]]
        self.assertEqual(names[-1], "Linha Lotada")  # lotada vai por último
        mixed = next(s for s in item["suggestions"] if s["name"] == "Linha Mista")
        self.assertTrue(mixed["already_on_route"])
        self.assertEqual(mixed["stop"], "Centro – Rua A")
        self.assertLess(mixed["distance_m"], 150)
        self.assertEqual(self.manager_api.get(self.URL, {"status": "reviewed"}).json(), [])

    def test_aprovar_troca_endereco_inclui_na_rota_e_atualiza_embarque(self):
        r = self.manager_api.post(f"{self.URL}{self.pending.id}/approve/", {"route_id": self.centro_route.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.pending.refresh_from_db()
        self.old.refresh_from_db()
        self.assertTrue(self.pending.is_active)
        self.assertEqual((self.pending.review_status, self.pending.reviewed_by), ("approved", self.manager_person))
        self.assertFalse(self.old.is_active)

        new_ride = RoutePassenger.objects.get(person=self.employee_person, route=self.centro_route)
        self.assertEqual(new_ride.route_stop.meeting_point.name, "Centro – Rua A")
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.route_stop, self.mixed_centro)  # embarque da rota atual foi para a parada mais perto
        # Linha Mista (parada mais perto), Rota A (sem paradas: embarque em casa) e a inclusão na Linha Centro.
        self.assertEqual(len(r.json()["changes"]), 3)

        notice = Notification.objects.get(recipient=self.employee_person, kind=Notification.Kind.ADDRESS_APPROVED)
        self.assertIn("Linha Centro", notice.message)
        again = self.manager_api.post(f"{self.URL}{self.pending.id}/approve/", {}, format="json")
        self.assertEqual(again.status_code, 400)

    def test_aprovar_sem_mexer_nas_rotas(self):
        r = self.manager_api.post(f"{self.URL}{self.pending.id}/approve/", {"update_current_stops": False}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.ride.refresh_from_db()
        self.assertEqual(self.ride.route_stop, self.mixed_norte)
        self.assertEqual(r.json()["changes"], [])

    def test_rota_lotada_nao_aprova_nada(self):
        r = self.manager_api.post(f"{self.URL}{self.pending.id}/approve/", {"route_id": self.full_route.id}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("lotada", r.json()["detail"])
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.review_status, "pending")  # transação desfeita

    def test_recusar_exige_motivo_e_mantem_o_anterior(self):
        self.assertEqual(self.manager_api.post(f"{self.URL}{self.pending.id}/reject/", {"reason": " "}, format="json").status_code, 400)
        r = self.manager_api.post(f"{self.URL}{self.pending.id}/reject/", {"reason": "Endereço da empresa, não da casa."}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.old.refresh_from_db()
        self.assertTrue(self.old.is_active)
        notice = Notification.objects.get(recipient=self.employee_person, kind=Notification.Kind.ADDRESS_REJECTED)
        self.assertIn("Endereço da empresa", notice.message)
        me = self.client_for(self.employee_person).get("/api/me/address/").json()
        self.assertEqual(me["rejected"]["review_note"], "Endereço da empresa, não da casa.")
        self.assertEqual(me["address"]["id"], self.old.id)

    def test_escopo_e_permissao(self):
        other_manager = self.employee(self.other_company, "Gestor Beta", "MB01")
        account = other_manager.access_account
        account.role = AccessAccount.Role.MANAGER
        account.save(update_fields=["role", "updated_at"])
        self.assertEqual(self.client_for(other_manager).get(self.URL).json(), [])
        self.assertEqual(self.client_for(other_manager).post(f"{self.URL}{self.pending.id}/approve/").status_code, 404)
        self.assertEqual(self.client_for(self.employee_person).get(self.URL).status_code, 403)

    def test_ferramentas_do_assistente(self):
        planner = Planner(PlanningConversation.objects.create(created_by=self.manager_person, company=self.company))
        [pending] = planner.pending_address_reviews()["pending"]
        self.assertEqual(pending["person_id"], self.employee_person.id)
        result = planner.suggest_fixed_routes(self.employee_person.id)
        self.assertEqual(result["address_status"], "aguardando aprovação")
        self.assertEqual(result["suggestions"][0]["stop"], "Centro – Rua A")

    def test_sem_rota_perto_mostra_as_mais_proximas_marcadas(self):
        far_home = address(-3.2500, -60.2000, person=self.manager_person, source=Address.Source.TYPED, is_active=False, review_status=Address.Review.PENDING)
        item = self.manager_api.get(f"{self.URL}{far_home.id}/").json()
        self.assertTrue(item["suggestions"])
        self.assertTrue(all(s["far"] or s["already_on_route"] for s in item["suggestions"]))

    def pending_at(self, lat, lng):
        self.pending.review_status = Address.Review.REJECTED
        self.pending.save(update_fields=["review_status"])
        return address(lat, lng, person=self.employee_person, source=Address.Source.GPS, is_active=False, review_status=Address.Review.PENDING)

    def test_ponto_da_empresa_perto_vira_parada_da_rota(self):
        leste = MeetingPoint.objects.create(company=self.company, name="Leste – Rua C", address=address(-3.1300, -60.0080, company=self.company))
        home = self.pending_at(-3.1303, -60.0083)  # ~45 m do ponto Leste e ~1,3 km do Centro (parada das rotas)
        centro = next(s for s in self.manager_api.get(f"{self.URL}{home.id}/").json()["suggestions"] if s["name"] == "Linha Centro")
        self.assertEqual((centro["pickup"], centro["pickup_point"]), ("new_point", "Leste – Rua C"))

        r = self.manager_api.post(f"{self.URL}{home.id}/approve/", {"route_id": self.centro_route.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        ride = RoutePassenger.objects.get(person=self.employee_person, route=self.centro_route)
        self.assertEqual(ride.route_stop.meeting_point, leste)
        sequences = list(self.centro_route.stops.order_by("sequence").values_list("sequence", flat=True))
        self.assertEqual(sequences, [0, 1])
        self.assertFalse(MeetingPoint.objects.filter(home_of=self.employee_person).exists())

    def test_sem_ponto_a_500m_embarca_na_porta_de_casa(self):
        home = self.pending_at(CENTRO[0] + 0.0080, CENTRO[1])  # ~890 m do ponto mais perto
        centro = next(s for s in self.manager_api.get(f"{self.URL}{home.id}/").json()["suggestions"] if s["name"] == "Linha Centro")
        self.assertEqual(centro["pickup"], "home")

        r = self.manager_api.post(f"{self.URL}{home.id}/approve/", {"route_id": self.centro_route.id}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        ride = RoutePassenger.objects.get(person=self.employee_person, route=self.centro_route)
        self.assertEqual(ride.route_stop.meeting_point.home_of, self.employee_person)
        self.assertEqual(ride.route_stop.meeting_point.address, home)
        self.assertTrue(any("porta da sua casa" in change for change in r.json()["changes"]))
        mine = self.client_for(self.employee_person).get("/api/me/routes/").json()
        self.assertIn("Sua casa", {item["stop"]["title"] for item in mine if item["stop"]})
        from core.self_address import nearest_point
        self.assertEqual(nearest_point(self.company, home.latitude, home.longitude)["name"], "Centro – Rua A")

        # Muda para perto do Centro: volta para a parada pública e a parada da casa sai das rotas que têm uma perto.
        near = address(*NEW_HOME, person=self.employee_person, source=Address.Source.GPS, is_active=False, review_status=Address.Review.PENDING)
        self.assertEqual(self.manager_api.post(f"{self.URL}{near.id}/approve/", {}, format="json").status_code, 200)
        ride.refresh_from_db()
        self.assertEqual(ride.route_stop.meeting_point.name, "Centro – Rua A")
        home_routes = set(RouteStop.objects.filter(meeting_point__home_of=self.employee_person).values_list("route__name", flat=True))
        # A Rota A não tinha paradas: em vez da casa, ganhou o ponto Centro (a 80 m da casa nova).
        self.assertEqual(home_routes, set())
        self.assertIn("Centro – Rua A", RoutePassenger.objects.get(person=self.employee_person, route=self.route).route_stop.meeting_point.name)
        self.assertEqual(list(self.mixed_route.stops.order_by("sequence").values_list("sequence", flat=True)), [0, 1])

    def test_comando_adiciona_pontos_reais_sem_duplicar(self):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command("add_manaus_meeting_points", company=[self.company.name], stdout=out)
        added = MeetingPoint.objects.filter(company=self.company, home_of__isnull=True).count()
        self.assertGreater(added, 50)
        call_command("add_manaus_meeting_points", company=[self.company.name], stdout=out)
        self.assertEqual(MeetingPoint.objects.filter(company=self.company, home_of__isnull=True).count(), added)
        self.assertIn("+0 pontos", out.getvalue())

    def test_comando_aplica_regra_aos_passageiros_antigos(self):
        from io import StringIO

        from django.core.management import call_command
        # Fulano mora perto do Norte e embarca no Norte (mantém); a gestora mora perto do Centro mas embarca longe.
        address(*NEW_HOME, person=self.manager_person)
        self.ride_manager = RoutePassenger.objects.create(person=self.manager_person, route=self.mixed_route, route_stop=self.mixed_norte)
        out = StringIO()
        call_command("apply_pickup_rule", "--dry-run", stdout=out)
        self.assertIn("Simulação", out.getvalue())
        self.ride_manager.refresh_from_db()
        self.assertEqual(self.ride_manager.route_stop, self.mixed_norte)  # simulação não grava

        call_command("apply_pickup_rule", "--notify", "--drop-empty-stops", stdout=out)
        self.ride_manager.refresh_from_db()
        self.ride.refresh_from_db()
        self.assertEqual(self.ride_manager.route_stop, self.mixed_centro)
        self.assertEqual(self.ride.route_stop, self.mixed_norte)
        notice = Notification.objects.filter(recipient=self.manager_person, kind=Notification.Kind.PICKUP_CHANGED).first()
        self.assertIn("Centro – Rua A", notice.message)
        self.assertIn("Norte – Rua B", notice.message)
        self.assertFalse(self.centro_route.stops.exists())  # ninguém embarcava: parada removida
        self.assertIn("vazias removidas", out.getvalue())
