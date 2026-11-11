"""Sugestão de pontos de embarque e mapa de demanda com H3."""
from datetime import time
from unittest import mock

from django.test import override_settings

from core.models import MeetingPoint, PlanningConversation, RoutePassenger, Schedule
from core.pickup_points import demand_map, suggest_pickup_points
from core.planning.planner import Planner
from core.tests import RoleTestCase
from core.tests_planning import address

POINT = (-3.100, -60.000)
# Bairro sem ponto, ~3,3 km ao norte do ponto existente.
FAR_CLUSTER = (-3.070, -60.000)
# Grupo pequeno longe de tudo, ~5,5 km a leste.
LONELY = (-3.100, -59.950)
STEP = 0.0015  # ~170 m entre casas vizinhas

GEOCODED = {"street": "Rua Nova", "number": "10", "sublocality": "Bairro Norte", "formatted": "Rua Nova, 10"}


class PickupPointsBase(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        MeetingPoint.objects.create(company=cls.company, name="Ponto Centro", address=address(*POINT, company=cls.company))
        cls.covered = []
        for index in range(4):
            person = cls.employee(cls.company, f"Perto {index}", f"P{index:02d}")
            address(POINT[0] + STEP * index / 2, POINT[1], person=person)
            cls.covered.append(person)
        cls.cluster = []
        for index in range(6):
            person = cls.employee(cls.company, f"Norte {index}", f"N{index:02d}")
            address(FAR_CLUSTER[0] + STEP * (index % 3), FAR_CLUSTER[1] + STEP * (index // 3), person=person)
            cls.cluster.append(person)
        cls.lonely = []
        for index in range(2):
            person = cls.employee(cls.company, f"Leste {index}", f"L{index:02d}")
            address(LONELY[0], LONELY[1] + STEP * index, person=person)
            cls.lonely.append(person)


@mock.patch("core.pickup_points.reverse_geocode", return_value=GEOCODED)
class SuggestPickupPointsTests(PickupPointsBase):
    def test_sugere_ponto_para_o_grupo_descoberto(self, _geocode):
        result = suggest_pickup_points(self.company)
        self.assertEqual(result["uncovered"], 8)  # 6 do norte + 2 isolados
        self.assertEqual(result["still_uncovered"], 2)  # os isolados não chegam a 5 pessoas
        [suggestion] = result["suggestions"]
        self.assertEqual(set(suggestion["person_ids"]), {p.id for p in self.cluster})
        self.assertEqual(suggestion["address"], GEOCODED)
        self.assertGreater(suggestion["avg_distance_now_m"], 3000)
        self.assertLess(suggestion["avg_distance_after_m"], 300)
        self.assertAlmostEqual(suggestion["latitude"], FAR_CLUSTER[0] + STEP, places=3)

    def test_limites_configuraveis(self, _geocode):
        self.assertEqual(len(suggest_pickup_points(self.company, min_people=7)["suggestions"]), 0)
        everyone = suggest_pickup_points(self.company, walk_limit=10000)
        self.assertEqual(everyone["uncovered"], 0)

    def test_restringe_aos_colaboradores_informados(self, _geocode):
        result = suggest_pickup_points(self.company, [p.id for p in self.cluster[:3]])
        self.assertEqual(result["employees"], 3)
        self.assertEqual(result["suggestions"], [])  # 3 < 5

    def test_ferramenta_do_assistente_nao_expoe_ids(self, _geocode):
        conversation = PlanningConversation.objects.create(created_by=self.manager_person, company=self.company)
        result = Planner(conversation).suggest_pickup_points(None)
        [suggestion] = result["suggestions"]
        self.assertNotIn("person_ids", suggestion)
        self.assertEqual(len(suggestion["names"]), 6)


class DemandMapTests(PickupPointsBase):
    def test_conta_por_hexagono(self):
        cells = demand_map(self.company)
        self.assertEqual(sum(c["count"] for c in cells), 12)  # quem não tem endereço (gestor, Fulano) não conta
        self.assertEqual([c["count"] for c in cells], sorted((c["count"] for c in cells), reverse=True))
        self.assertEqual(len(cells[0]["boundary"]), 6)

    def test_filtra_por_turno(self):
        schedule = Schedule.objects.create(company=self.company, weekdays=["MON"], time=time(6, 0))
        self.route.schedule = schedule
        self.route.save()
        for person in self.cluster:
            RoutePassenger.objects.create(person=person, route=self.route)
        cells = demand_map(self.company, schedule_id=schedule.pk)
        self.assertEqual(sum(c["count"] for c in cells), 6)


@override_settings(GOOGLE_MAPS_API_KEY="")
class PickupApiTests(PickupPointsBase):
    def test_gestor_consulta_sugestoes_e_mapa(self):
        client = self.client_for(self.manager_person)
        r = client.get("/api/pickup-suggestions/?min_people=5")
        self.assertEqual(r.status_code, 200, r.content)
        [suggestion] = r.json()["suggestions"]
        self.assertIsNone(suggestion["address"])  # sem chave do Google, só coordenadas
        r = client.get("/api/demand-map/?resolution=9")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["resolution"], 9)

    def test_validacoes_e_permissao(self):
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get("/api/pickup-suggestions/?walk_limit=50").status_code, 400)
        foreign = Schedule.objects.create(company=self.other_company, weekdays=["MON"], time=time(7, 0))
        self.assertEqual(client.get(f"/api/demand-map/?schedule={foreign.pk}").status_code, 400)
        self.assertEqual(self.client_for(self.driver_person).get("/api/pickup-suggestions/").status_code, 403)


@mock.patch("core.pickup_points.reverse_geocode", return_value=GEOCODED)
class NoMeetingPointsTests(PickupPointsBase):
    """Empresa recém-importada, sem nenhum ponto de encontro: a sugestão funciona (antes dava erro interno)."""

    def setUp(self):
        super().setUp()
        MeetingPoint.objects.filter(company=self.company).delete()

    def test_sugere_pontos_sem_distancia_atual(self, _geocode):
        result = suggest_pickup_points(self.company)
        self.assertEqual(result["covered"], 0)
        self.assertGreaterEqual(len(result["suggestions"]), 1)
        for suggestion in result["suggestions"]:
            self.assertIsNone(suggestion["avg_distance_now_m"])
            self.assertIsInstance(suggestion["avg_distance_after_m"], int)

    def test_ferramenta_do_assistente_tambem_funciona(self, _geocode):
        conversation = PlanningConversation.objects.create(created_by=self.manager_person, company=self.company)
        self.assertGreaterEqual(len(Planner(conversation).suggest_pickup_points(None)["suggestions"]), 1)
