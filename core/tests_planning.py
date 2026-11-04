"""Assistente de planejamento de rotas avulsas: ferramentas, aprovação, loop do agente e API."""
import copy
import json
from datetime import time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings

from core.dates import local_today
from core.models import (
    AccessAccount,
    Address,
    MeetingPoint,
    Notification,
    PlanningConversation,
    Route,
    RoutePassenger,
    Schedule,
    Vehicle,
)
from core.planning.agent import CONTEXT_PREFIX, PlanningAgent, transcript
from core.planning.planner import Planner, PlanningError
from core.tests import RoleTestCase

DEPOT = (-3.1135, -59.9675)
POINTS = {"Norte": (-3.030, -59.980), "Sul": (-3.130, -60.010), "Oeste": (-3.100, -60.060)}
# ~200 m do ponto, para cada colaborador embarcar no ponto do seu bairro.
NEAR = 0.0018


def address(lat, lng, **owner):
    return Address.objects.create(
        street="Rua Teste", number="1", city="Manaus", state="AM", zip_code="69000-000",
        latitude=Decimal(f"{lat:.6f}"), longitude=Decimal(f"{lng:.6f}"), **owner,
    )


class PlanningBase(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        address(*DEPOT, company=cls.company)
        cls.points = {
            name: MeetingPoint.objects.create(
                company=cls.company, name=f"{name} – Rua Teste", address=address(lat, lng, company=cls.company)
            )
            for name, (lat, lng) in POINTS.items()
        }
        cls.riders = []
        for bairro, count in (("Norte", 6), ("Sul", 5), ("Oeste", 4)):
            lat, lng = POINTS[bairro]
            for index in range(count):
                person = cls.employee(cls.company, f"Pessoa {bairro} {index + 1}", f"{bairro[0]}{index + 1:02d}")
                address(lat + NEAR, lng, person=person)
                cls.riders.append(person)
        cls.jose = cls.employee(cls.company, "José Araújo", "J01")
        address(POINTS["Sul"][0], POINTS["Sul"][1] + NEAR, person=cls.jose)
        cls.joana = cls.employee(cls.company, "Joana Silva", "J02")
        cls.joao = cls.employee(cls.company, "João Silva", "J03")
        cls.foreigner = cls.employee(cls.other_company, "Pessoa Norte 1", "X01")

        # Motorista Um (capacidade 10, rota fixa sem turno) + dois motoristas novos.
        cls.second_driver = cls.driver(cls.company, "Motorista Dois Acme", "D010")
        Vehicle.objects.create(company=cls.company, driver=cls.second_driver, plate_number="ACM-0010", model="Ônibus", capacity=10)
        cls.busy_driver = cls.driver(cls.company, "Motorista Ocupado", "D011")
        busy_vehicle = Vehicle.objects.create(
            company=cls.company, driver=cls.busy_driver, plate_number="ACM-0011", model="Van", capacity=6
        )
        every_day = Schedule.objects.create(
            company=cls.company, time=time(6, 0), weekdays=["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
        )
        Route.objects.create(company=cls.company, name="Fixa 06h", vehicle=busy_vehicle, schedule=every_day)

    def setUp(self):
        super().setUp()
        self.conversation = PlanningConversation.objects.create(created_by=self.manager_person, company=self.company)
        self.planner = Planner(self.conversation)
        self.day = (local_today() + timedelta(days=1)).isoformat()

    def plan(self, max_routes=2, arrival="06:00", people=None):
        return self.planner.plan_routes(self.day, arrival, [p.id for p in (people or self.riders)], max_routes)


class FindEmployeesTests(PlanningBase):
    def test_por_matricula_email_e_nome_sem_acento(self):
        result = self.planner.find_employees(["N01", "s02@acme.com", "jose araujo"])
        self.assertEqual({e["name"] for e in result["found"]}, {"Pessoa Norte 1", "Pessoa Sul 2", "José Araújo"})
        self.assertEqual(result["not_found"], [])

    def test_nome_ambiguo_e_nao_encontrado(self):
        result = self.planner.find_employees(["silva", "Fulana Inexistente"])
        [ambiguous] = result["ambiguous"]
        self.assertEqual({c["name"] for c in ambiguous["candidates"]}, {"Joana Silva", "João Silva"})
        self.assertEqual(result["not_found"], ["Fulana Inexistente"])

    def test_nome_exato_vence_sobrenomes_trocados(self):
        self.employee(self.company, "Silva Joana", "J04")
        result = self.planner.find_employees(["Joana Silva"])
        self.assertEqual([e["code"] for e in result["found"]], ["J02"])

    def test_nao_ve_outra_empresa(self):
        result = self.planner.find_employees(["X01"])
        self.assertEqual(result["not_found"], ["X01"])


class DriversAndPlanTests(PlanningBase):
    def test_motorista_com_rota_proxima_fica_ocupado(self):
        morning = self.planner.available_drivers(self.day, "07:00")
        self.assertIn("Motorista Ocupado", {d["name"] for d in morning["busy"]})
        afternoon = self.planner.available_drivers(self.day, "14:00")
        self.assertIn("Motorista Ocupado", {d["name"] for d in afternoon["available"]})

    def test_plano_cobre_todos_dentro_da_capacidade(self):
        plan = self.plan(max_routes=2)
        self.assertEqual(plan["total_passengers"], 15)
        self.assertLessEqual(len(plan["routes"]), 2)
        planned = {p["id"] for r in plan["routes"] for s in r["stops"] for p in s["passengers"]}
        self.assertEqual(planned, {p.id for p in self.riders})
        for route in plan["routes"]:
            self.assertLessEqual(route["load"], route["vehicle"]["capacity"])
            self.assertNotEqual(route["driver"]["name"], "Motorista Ocupado")
            etas = [s["eta"] for s in route["stops"]]
            self.assertEqual(etas, sorted(etas))
            self.assertLess(etas[-1], "06:00")
            self.assertLess(route["departure"], etas[0])

    def test_com_folga_usa_mais_onibus_para_encurtar_a_viagem(self):
        # 14:00: os três motoristas estão livres.
        self.assertEqual(len(self.plan(max_routes=3, arrival="14:00")["routes"]), 2)  # 2 já cabem na meta de tempo
        with mock.patch("core.planning.planner.TARGET_RIDE_MINUTES", 1):
            plan = self.plan(max_routes=3, arrival="14:00")
        self.assertEqual(len(plan["routes"]), 3)
        self.assertTrue(any("mesmo usando todas as rotas" in w for w in plan["warnings"]))

    def test_nao_cabe_no_limite_de_rotas(self):
        with self.assertRaisesMessage(PlanningError, "Não cabe"):
            self.plan(max_routes=1)

    def test_data_passada(self):
        yesterday = (local_today() - timedelta(days=1)).isoformat()
        with self.assertRaisesMessage(PlanningError, "já passou"):
            self.planner.plan_routes(yesterday, "06:00", [self.riders[0].id], 1)

    def test_id_de_outra_empresa_e_recusado(self):
        with self.assertRaisesMessage(PlanningError, "não são colaboradores ativos"):
            self.planner.plan_routes(self.day, "06:00", [self.foreigner.id], 1)

    def test_sem_endereco_vira_aviso(self):
        plan = self.plan(people=self.riders + [self.joana])
        self.assertTrue(any("Joana Silva" in w for w in plan["warnings"]))
        self.assertEqual(plan["total_passengers"], 15)


def group(scope="all", schedule_arrival=None, bairros=None, exclude=None):
    return {"scope": scope, "schedule_arrival": schedule_arrival, "bairros": bairros, "exclude": exclude}


class GroupSelectionTests(PlanningBase):
    def test_todos_da_empresa(self):
        result = self.planner.select_employees(group())
        ids = {p["id"] for p in result["people"]}
        self.assertTrue({p.id for p in self.riders + [self.jose, self.joana]} <= ids)
        self.assertNotIn(self.foreigner.id, ids)
        self.assertEqual(result["total"], len(ids))
        self.assertIn("Joana Silva", result["without_address"])
        self.assertEqual(result["with_address"] + len(result["without_address"]), result["total"])
        by_point = {item["point"]: item["people"] for item in result["by_meeting_point"]}
        self.assertGreaterEqual(by_point["Oeste"], 4)

    def test_bairros_com_excecoes(self):
        result = self.planner.select_employees(group("bairros", bairros=["norte", "Oeste"], exclude=["N01", "Silva", "Ninguém"]))
        names = {p["name"] for p in result["people"]}
        self.assertTrue({f"Pessoa Norte {i}" for i in range(2, 7)} | {f"Pessoa Oeste {i}" for i in range(1, 5)} <= names)
        self.assertNotIn("Pessoa Norte 1", names)
        self.assertFalse(any(name.startswith("Pessoa Sul") for name in names))
        self.assertEqual(len(result["notes"]), 2)  # Silva (sem endereço, fora do grupo) e Ninguém
        with self.assertRaisesMessage(PlanningError, "Bairros com ponto de encontro: Norte, Oeste, Sul"):
            self.planner.select_employees(group("bairros", bairros=["Centro"]))

    def test_turno_fixo(self):
        afternoon = Schedule.objects.create(company=self.company, time=time(14, 0), weekdays=["MON"])
        route = Route.objects.create(
            company=self.company, name="Fixa 14h", vehicle=Vehicle.objects.get(plate_number="ACM-0010"), schedule=afternoon
        )
        RoutePassenger.objects.create(person=self.riders[0], route=route)
        result = self.planner.select_employees(group("schedule", schedule_arrival="14:00"))
        self.assertEqual([p["id"] for p in result["people"]], [self.riders[0].id])
        with self.assertRaisesMessage(PlanningError, "Turnos com rota fixa: 06:00, 14:00"):
            self.planner.select_employees(group("schedule", schedule_arrival="09:00"))

    def test_plano_com_grupo_sem_passar_ids(self):
        plan = self.planner.plan_routes(self.day, "14:00", None, 3, group=group(exclude=["José Araújo"]))
        planned = {p["id"] for r in plan["routes"] for s in r["stops"] for p in s["passengers"]}
        self.assertTrue({p.id for p in self.riders} <= planned)
        self.assertNotIn(self.jose.id, planned)
        self.assertTrue(any("Joana Silva" in w for w in plan["warnings"]))

    def test_sem_lista_nem_grupo(self):
        with self.assertRaisesMessage(PlanningError, "person_ids"):
            self.planner.plan_routes(self.day, "14:00", None, 2)


class AdjustAndApproveTests(PlanningBase):
    def test_mover_passageiro_e_respeitar_capacidade(self):
        plan = self.plan(max_routes=2)
        source, target = plan["routes"][0], plan["routes"][1]
        moved = source["stops"][0]["passengers"][0]["id"]
        if target["load"] < target["vehicle"]["capacity"]:
            after = self.planner.move_passengers([moved], 2)
            ids_route_2 = {p["id"] for s in after["routes"][1]["stops"] for p in s["passengers"]}
            self.assertIn(moved, ids_route_2)
        everyone_else = [p.id for p in self.riders]
        with self.assertRaisesMessage(PlanningError, "o ônibus leva"):
            self.planner.move_passengers(everyone_else, 1)

    def test_trocar_motorista_ocupado_e_recusado(self):
        self.plan(max_routes=2)
        with self.assertRaisesMessage(PlanningError, "ocupado"):
            self.planner.change_driver(1, self.busy_driver.id)

    def test_aprovar_cria_rotas_do_dia_e_avisa(self):
        plan = self.plan(max_routes=2)
        routes = self.planner.approve()
        self.assertEqual(len(routes), len(plan["routes"]))
        for route in routes:
            self.assertEqual(route.service_date.isoformat(), self.day)
            self.assertEqual(route.schedule.time, time(6, 0))
            self.assertTrue(route.name.startswith("Extra "))
        self.assertEqual(RoutePassenger.objects.filter(route__in=routes).count(), 15)
        assigned = Notification.objects.filter(kind=Notification.Kind.ROUTE_ASSIGNED)
        self.assertEqual(assigned.filter(recipient__in=self.riders).count(), 15)
        self.assertEqual(assigned.filter(recipient__type="DRIVER").count(), len(routes))
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, PlanningConversation.Status.APPROVED)
        with self.assertRaisesMessage(PlanningError, "já foi aprovado"):
            self.planner.approve()

    def test_rota_avulsa_aparece_ate_o_dia_dela(self):
        self.plan(max_routes=2)
        [route, *_] = self.planner.approve()
        driver = route.vehicle.driver
        client = self.client_for(driver)
        names = [r["name"] for r in client.get("/api/routes/my-routes/").json()]
        self.assertIn(route.name, names)
        item = next(r for r in client.get("/api/routes/my-routes/").json() if r["name"] == route.name)
        self.assertEqual(item["service_date"], self.day)
        with mock.patch("core.viewsets.local_today", return_value=local_today() + timedelta(days=2)):
            names = [r["name"] for r in client.get("/api/routes/my-routes/").json()]
        self.assertNotIn(route.name, names)


class FakeBlock(SimpleNamespace):
    def model_dump(self, **kwargs):
        return dict(vars(self))


def fake_response(blocks, stop_reason):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=0)
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, usage=usage)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


class AgentLoopTests(PlanningBase):
    @override_settings(PLANNING_MODEL="claude-opus-5-5")
    def test_chama_ferramenta_e_responde(self):
        client = FakeClient([
            fake_response([FakeBlock(type="tool_use", id="tu_1", name="find_employees", input={"identifiers": ["N01", "zzz"]})], "tool_use"),
            fake_response([FakeBlock(type="text", text="Achei 1; 'zzz' não existe.")], "end_turn"),
        ])
        answer = PlanningAgent(self.conversation, client=client).reply("Rotas para N01 e zzz amanhã às 6h")
        self.assertEqual(answer, "Achei 1; 'zzz' não existe.")

        first, second = client.calls
        self.assertEqual(first["model"], "claude-opus-5-5")
        self.assertEqual(first["fallbacks"], "default")
        self.assertTrue(all(tool["strict"] for tool in first["tools"]))
        self.assertTrue(first["messages"][-1]["content"][0]["text"].startswith(CONTEXT_PREFIX))
        tool_result = second["messages"][-1]["content"][0]
        self.assertEqual(tool_result["tool_use_id"], "tu_1")
        self.assertEqual(json.loads(tool_result["content"])["not_found"], ["zzz"])

        self.conversation.refresh_from_db()
        self.assertEqual(len(self.conversation.messages), 4)
        self.assertEqual(self.conversation.input_tokens, 300)
        self.assertEqual(self.conversation.output_tokens, 40)
        self.assertEqual(
            transcript(self.conversation),
            [
                {"role": "user", "text": "Rotas para N01 e zzz amanhã às 6h"},
                {"role": "assistant", "text": "Achei 1; 'zzz' não existe."},
            ],
        )

    def test_erro_de_regra_volta_como_erro_da_ferramenta(self):
        client = FakeClient([
            fake_response([FakeBlock(type="tool_use", id="tu_1", name="show_plan", input={}),
                           FakeBlock(type="tool_use", id="tu_2", name="change_driver", input={"route_number": 1, "driver_id": 1})], "tool_use"),
            fake_response([FakeBlock(type="text", text="Ainda não há plano.")], "end_turn"),
        ])
        PlanningAgent(self.conversation, client=client).reply("troca o motorista")
        results = client.calls[1]["messages"][-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["tu_1", "tu_2"])
        self.assertTrue(results[1]["is_error"])
        self.assertIn("plan_routes", results[1]["content"])

    def test_grupo_inteiro_pelo_agente(self):
        client = FakeClient([
            fake_response([FakeBlock(type="tool_use", id="tu_1", name="select_employees", input={"group": group()})], "tool_use"),
            fake_response([FakeBlock(type="text", text="São todos os colaboradores.")], "end_turn"),
        ])
        PlanningAgent(self.conversation, client=client).reply("todos os colaboradores amanhã às 14h")
        self.assertIn("select_employees", {tool["name"] for tool in client.calls[0]["tools"]})
        result = json.loads(client.calls[1]["messages"][-1]["content"][0]["content"])
        self.assertGreaterEqual(result["total"], len(self.riders) + 3)

    @override_settings(PLANNING_MODEL="claude-haiku-5-5")
    def test_haiku_sem_reserva_no_servidor(self):
        client = FakeClient([fake_response([FakeBlock(type="text", text="Qual o horário?")], "end_turn")])
        PlanningAgent(self.conversation, client=client).reply("rotas amanhã")
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertNotIn("fallbacks", call)
        self.assertNotIn("betas", call)

    def test_recusa(self):
        client = FakeClient([fake_response([], "refusal")])
        answer = PlanningAgent(self.conversation, client=client).reply("algo fora do escopo")
        self.assertIn("Não consegui", answer)


class PlanningApiTests(PlanningBase):
    def test_gestor_cria_e_consulta(self):
        client = self.client_for(self.manager_person)
        r = client.post("/api/planning/")
        self.assertEqual(r.status_code, 201, r.content)
        detail = client.get(f"/api/planning/{r.json()['id']}/").json()
        self.assertEqual(detail["status"], "draft")
        self.assertIsNone(detail["plan"])

    def test_so_gestor_acessa(self):
        self.assertEqual(self.client_for(self.driver_person).post("/api/planning/").status_code, 403)
        self.assertEqual(self.client_for(self.employee_person).get("/api/planning/").status_code, 403)

    def test_gestor_de_outra_empresa_nao_ve(self):
        other_manager = self.employee(self.other_company, "Gestor Beta", "MB01")
        account = other_manager.access_account
        account.role = AccessAccount.Role.MANAGER
        account.save(update_fields=["role", "updated_at"])
        r = self.client_for(other_manager).get(f"/api/planning/{self.conversation.pk}/")
        self.assertEqual(r.status_code, 404)

    @override_settings(ANTHROPIC_API_KEY="")
    def test_sem_chave_responde_503(self):
        r = self.client_for(self.manager_person).post(
            f"/api/planning/{self.conversation.pk}/messages/", {"text": "oi"}, format="json"
        )
        self.assertEqual(r.status_code, 503)

    def test_mensagem_usa_o_agente(self):
        client = FakeClient([fake_response([FakeBlock(type="text", text="Qual o horário de chegada?")], "end_turn")])
        with mock.patch("core.planning.views.PlanningAgent", lambda conv: PlanningAgent(conv, client=client)):
            r = self.client_for(self.manager_person).post(
                f"/api/planning/{self.conversation.pk}/messages/", {"text": "rotas amanhã"}, format="json"
            )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["reply"], "Qual o horário de chegada?")
        self.assertEqual(len(r.json()["messages"]), 2)

    def test_aprovar_pela_api(self):
        self.plan(max_routes=2)
        client = self.client_for(self.manager_person)
        r = client.post(f"/api/planning/{self.conversation.pk}/approve/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["status"], "approved")
        self.assertTrue(r.json()["routes"])
        again = client.post(f"/api/planning/{self.conversation.pk}/approve/")
        self.assertEqual(again.status_code, 400)
        blocked = client.post(f"/api/planning/{self.conversation.pk}/messages/", {"text": "mais"}, format="json")
        self.assertEqual(blocked.status_code, 409)
