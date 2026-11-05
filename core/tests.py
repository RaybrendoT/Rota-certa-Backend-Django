from decimal import Decimal
from io import StringIO

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

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
    derive_email_domain_from_company_name,
)
from core.tokens import AccessAccountRefreshToken

FAST_HASHERS = override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)

# Os testes partem da regra de senha inicial (matrícula + 4 últimos dígitos do telefone).
# Um ACCESS_ACCOUNT_AUTO_INITIAL_PASSWORD no .env de quem roda trocaria todas as senhas
# por uma fixa e quebraria os logins; no CI não há .env, por isso só falhava localmente.
INITIAL_PASSWORD_RULE = override_settings(ACCESS_ACCOUNT_AUTO_INITIAL_PASSWORD="")


class EmailDomainDerivationTests(TestCase):
    """Domínio automático (sem Company.email_domain explícito) a partir do nome."""

    def test_nome_curto_sai_inteiro(self):
        self.assertEqual(derive_email_domain_from_company_name("Coca-Cola"), "cocacola.com")

    def test_uma_palavra(self):
        self.assertEqual(derive_email_domain_from_company_name("Samsung"), "samsung.com")

    def test_acumula_enquanto_couber(self):
        self.assertEqual(
            derive_email_domain_from_company_name("Amazonia Logistica Norte"),
            "amazonialogisticanorte.com",
        )

    def test_para_antes_de_cortar_palavra_no_meio(self):
        # "distribuidora" (13) + "comercial" (9) = 22 cabe em 24; + "industrial" (10) estouraria.
        domain = derive_email_domain_from_company_name("Distribuidora Comercial Industrial Ltda")
        self.assertEqual(domain, "distribuidoracomercial.com")

    def test_respeita_max_length_customizado(self):
        self.assertEqual(
            derive_email_domain_from_company_name("Amazonia Logistica Norte", max_length=10),
            "amazonia.com",
        )

    def test_nome_vazio(self):
        self.assertEqual(derive_email_domain_from_company_name(""), "empresa.com")

    def test_ignora_pontuacao_e_acentos_na_palavra(self):
        self.assertEqual(derive_email_domain_from_company_name("Ponta Negra Serviços"), "pontanegraservios.com")


@FAST_HASHERS
@INITIAL_PASSWORD_RULE
class RoleTestCase(TestCase):
    """Base: duas empresas, gestor, dois motoristas (cada um com rota/veículo) e um funcionário."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Acme", email_domain="acme.com")
        cls.other_company = Company.objects.create(name="Beta", email_domain="beta.com")

        cls.manager_person = cls.employee(cls.company, "Maria Gestora", "M001")
        cls.manager = cls.manager_person.access_account
        cls.manager.role = AccessAccount.Role.MANAGER
        cls.manager.save(update_fields=["role", "updated_at"])

        cls.gerente_person = cls.employee(cls.company, "Gustavo Gerente", "G001")
        cls.gerente = cls.gerente_person.access_account
        cls.gerente.role = AccessAccount.Role.GERENTE
        cls.gerente.save(update_fields=["role", "updated_at"])

        cls.employee_person = cls.employee(cls.company, "Fulano", "E001")
        cls.driver_person = cls.driver(cls.company, "Motorista Um", "D001")
        cls.other_driver_person = cls.driver(cls.other_company, "Motorista Dois", "D002")

        cls.route = cls.make_route(cls.company, "Rota A", cls.driver_person, "AAA-0001")
        cls.other_route = cls.make_route(
            cls.other_company, "Rota B", cls.other_driver_person, "BBB-0002"
        )
        cls.passenger = RoutePassenger.objects.create(
            person=cls.employee_person, route=cls.route
        )
        cls.other_passenger = RoutePassenger.objects.create(
            person=cls.manager_person, route=cls.other_route
        )

    @staticmethod
    def employee(company, name, code):
        return Person.objects.create(
            company=company, name=name, phone="92999990000",
            type=Person.PersonType.EMPLOYEE, employee_code=code,
        )

    @staticmethod
    def driver(company, name, license_number):
        return Person.objects.create(
            company=company, name=name, phone="92999991111",
            type=Person.PersonType.DRIVER, license_number=license_number,
        )

    @staticmethod
    def make_route(company, name, driver, plate):
        vehicle = Vehicle.objects.create(
            company=company, driver=driver, plate_number=plate, model="Van", capacity=10
        )
        return Route.objects.create(company=company, name=name, vehicle=vehicle)

    def setUp(self):
        cache.clear()  # os throttles do DRF usam o cache

    def client_for(self, person):
        token = AccessAccountRefreshToken.for_access_account(person.access_account)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        return client

    def event_payload(self, route, **extra):
        return {
            "route": route.pk,
            "event_type": "departure",
            "latitude": "-3.100000",
            "longitude": "-60.000000",
            **extra,
        }


class AccessAccountRoleModelTests(RoleTestCase):
    def test_conta_nasce_com_perfil_derivado_do_tipo_da_pessoa(self):
        self.assertEqual(self.driver_person.access_account.role, AccessAccount.Role.DRIVER)
        self.assertEqual(self.employee_person.access_account.role, AccessAccount.Role.EMPLOYEE)

    def test_motorista_nao_pode_ser_gestor(self):
        account = self.driver_person.access_account
        account.role = AccessAccount.Role.MANAGER
        with self.assertRaises(ValidationError):
            account.save()
        with self.assertRaises(ValidationError):
            account.save(update_fields=["role"])
        account.refresh_from_db()
        self.assertEqual(account.role, AccessAccount.Role.DRIVER)

    def test_motorista_nao_pode_ser_gerente(self):
        account = self.driver_person.access_account
        account.role = AccessAccount.Role.GERENTE
        with self.assertRaises(ValidationError):
            account.save()
        account.refresh_from_db()
        self.assertEqual(account.role, AccessAccount.Role.DRIVER)

    def test_funcionario_nao_pode_ter_perfil_de_motorista(self):
        account = self.employee_person.access_account
        account.role = AccessAccount.Role.DRIVER
        with self.assertRaises(ValidationError):
            account.save()

    def test_salvar_outros_campos_nao_revalida_o_perfil(self):
        account = AccessAccount.objects.get(pk=self.manager.pk)  # sem person em cache
        with self.assertNumQueries(1):
            account.failed_login_attempts = 1
            account.save(update_fields=["failed_login_attempts"])

    def test_gestor_que_vira_motorista_perde_o_perfil_de_gestor(self):
        person = Person.objects.get(pk=self.manager_person.pk)
        person.type = Person.PersonType.DRIVER
        person.employee_code = None
        person.license_number = "CNH-9"
        person.save()
        self.assertEqual(person.access_account.role, AccessAccount.Role.DRIVER)

    def test_motorista_que_vira_funcionario_volta_a_funcionario(self):
        person = Person.objects.get(pk=self.driver_person.pk)
        person.type = Person.PersonType.EMPLOYEE
        person.license_number = None
        person.employee_code = "E-NEW"
        person.save()
        self.assertEqual(person.access_account.role, AccessAccount.Role.EMPLOYEE)

    def test_gestor_promovido_mantem_perfil_ao_editar_a_pessoa(self):
        person = Person.objects.get(pk=self.manager_person.pk)
        person.name = "Maria Silva"
        person.save()
        self.assertEqual(person.access_account.role, AccessAccount.Role.MANAGER)


class SessionPayloadTests(RoleTestCase):
    def login(self, person, password):
        return APIClient().post(
            "/api/auth/login/",
            {"email": person.access_account.email, "password": password},
            format="json",
        )

    def test_login_devolve_o_perfil(self):
        r = self.login(self.manager_person, "M0010000")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["person"]["role"], "MANAGER")

    def test_login_do_motorista_devolve_driver(self):
        r = self.login(self.driver_person, "D0011111")
        self.assertEqual(r.json()["person"]["role"], "DRIVER")

    def test_me_devolve_perfil_lido_do_banco(self):
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get("/api/auth/me/").json()["person"]["role"], "MANAGER")

        # rebaixado depois de emitir o token: o /me reflete o banco, não o token
        AccessAccount.objects.filter(pk=self.manager.pk).update(role="EMPLOYEE")
        self.assertEqual(client.get("/api/auth/me/").json()["person"]["role"], "EMPLOYEE")

    def test_me_exige_autenticacao(self):
        self.assertEqual(APIClient().get("/api/auth/me/").status_code, 401)


class ManagerPermissionTests(RoleTestCase):
    def test_sem_token_a_api_e_fechada(self):
        anon = APIClient()
        for url in ("/api/companies/", "/api/people/", "/api/routes/", "/api/vehicles/",
                    "/api/route-events/", "/api/route-passengers/"):
            self.assertEqual(anon.get(url).status_code, 401, url)

    def test_gestor_le_apenas_rotas_da_propria_empresa(self):
        client = self.client_for(self.manager_person)
        route_names = {r["name"] for r in client.get("/api/routes/").json()["results"]}
        self.assertEqual(route_names, {"Rota A"})

    def test_gestor_nao_ve_nem_edita_rota_de_outra_empresa(self):
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get(f"/api/routes/{self.other_route.pk}/").status_code, 404)
        self.assertEqual(
            client.patch(f"/api/routes/{self.other_route.pk}/", {"name": "X"}, format="json").status_code,
            404,
        )

    def test_gestor_nao_cria_recurso_em_outra_empresa(self):
        client = self.client_for(self.manager_person)
        r = client.post(
            "/api/routes/", {"company": self.other_company.pk, "name": "Rota Alheia"}, format="json"
        )
        self.assertEqual(r.status_code, 403, r.content)
        r = client.post(
            "/api/people/",
            {"company": self.other_company.pk, "name": "Intruso", "phone": "92988880000",
             "type": "EMPLOYEE", "employee_code": "E888"},
            format="json",
        )
        self.assertEqual(r.status_code, 403, r.content)

    def test_gestor_nao_move_recurso_para_outra_empresa(self):
        client = self.client_for(self.manager_person)
        r = client.patch(
            f"/api/routes/{self.route.pk}/", {"company": self.other_company.pk}, format="json"
        )
        self.assertEqual(r.status_code, 403, r.content)

    def test_gestor_nao_acessa_o_cadastro_de_empresas(self):
        # Empresas agora são exclusivas do gerente (IsGerente).
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get("/api/companies/").status_code, 403)
        self.assertEqual(
            client.post("/api/companies/", {"name": "Gama"}, format="json").status_code, 403
        )
        self.assertEqual(
            client.patch(f"/api/companies/{self.company.pk}/", {"name": "X"}, format="json").status_code,
            403,
        )
        self.assertEqual(client.delete(f"/api/companies/{self.company.pk}/").status_code, 403)

    def test_gestor_cadastra_pessoa_e_recebe_conta(self):
        client = self.client_for(self.manager_person)
        r = client.post(
            "/api/people/",
            {"company": self.company.pk, "name": "Novo", "phone": "92988887777",
             "type": "EMPLOYEE", "employee_code": "E777"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["role"], "EMPLOYEE")

    def test_people_lista_o_perfil(self):
        client = self.client_for(self.manager_person)
        by_name = {p["name"]: p["role"] for p in client.get("/api/people/").json()["results"]}
        self.assertEqual(by_name["Maria Gestora"], "MANAGER")
        self.assertEqual(by_name["Motorista Um"], "DRIVER")
        self.assertEqual(by_name["Fulano"], "EMPLOYEE")

    def test_gestor_inativo_perde_o_acesso(self):
        client = self.client_for(self.manager_person)
        AccessAccount.objects.filter(pk=self.manager.pk).update(is_active=False)
        self.assertEqual(client.get("/api/companies/").status_code, 401)

    def test_gestor_nao_usa_endpoints_exclusivos_do_motorista(self):
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get("/api/routes/my-routes/").status_code, 403)
        self.assertEqual(client.get(f"/api/routes/{self.route.pk}/navigation/").status_code, 403)


class CompanyScopeTests(RoleTestCase):
    """Escopo por empresa do gestor nos demais viewsets (veículos, horários, endereços, etc.)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other_employee_person = cls.employee(cls.other_company, "Funcionaria Beta", "E-BETA")

        cls.schedule = Schedule.objects.create(company=cls.company, weekdays=[1, 2, 3], time="08:00")
        cls.other_schedule = Schedule.objects.create(company=cls.other_company, weekdays=[1], time="09:00")

        cls.address = Address.objects.create(
            company=cls.company, street="Rua A", number="1", city="Manaus", state="AM",
            zip_code="69000-000", latitude="-3.1", longitude="-60.0",
        )
        cls.other_address = Address.objects.create(
            company=cls.other_company, street="Rua B", number="2", city="Manaus", state="AM",
            zip_code="69000-000", latitude="-3.1", longitude="-60.0",
        )
        cls.meeting_point = MeetingPoint.objects.create(company=cls.company, name="Ponto A", address=cls.address)
        cls.other_meeting_point = MeetingPoint.objects.create(
            company=cls.other_company, name="Ponto B", address=cls.other_address
        )
        cls.route_stop = RouteStop.objects.create(route=cls.route, meeting_point=cls.meeting_point, sequence=0)
        cls.other_route_stop = RouteStop.objects.create(
            route=cls.other_route, meeting_point=cls.other_meeting_point, sequence=0
        )

    def client_manager(self):
        return self.client_for(self.manager_person)

    def test_veiculos_escopados_por_empresa(self):
        client = self.client_manager()
        plates = {v["plate_number"] for v in client.get("/api/vehicles/").json()["results"]}
        self.assertEqual(plates, {"AAA-0001"})
        self.assertEqual(client.get(f"/api/vehicles/{self.other_route.vehicle.pk}/").status_code, 404)

    def test_horarios_escopados_por_empresa(self):
        client = self.client_manager()
        ids = {s["id"] for s in client.get("/api/schedules/").json()["results"]}
        self.assertEqual(ids, {self.schedule.pk})
        self.assertEqual(client.get(f"/api/schedules/{self.other_schedule.pk}/").status_code, 404)

    def test_pontos_de_encontro_escopados_por_empresa(self):
        client = self.client_manager()
        ids = {m["id"] for m in client.get("/api/meeting-points/").json()["results"]}
        self.assertEqual(ids, {self.meeting_point.pk})
        self.assertEqual(client.get(f"/api/meeting-points/{self.other_meeting_point.pk}/").status_code, 404)

    def test_enderecos_de_empresa_e_de_pessoa_escopados(self):
        client = self.client_manager()
        ids = {a["id"] for a in client.get("/api/addresses/").json()["results"]}
        self.assertIn(self.address.pk, ids)
        self.assertNotIn(self.other_address.pk, ids)
        self.assertEqual(client.get(f"/api/addresses/{self.other_address.pk}/").status_code, 404)

    def test_paradas_de_rota_escopadas_por_empresa(self):
        client = self.client_manager()
        ids = {s["id"] for s in client.get("/api/route-stops/").json()["results"]}
        self.assertEqual(ids, {self.route_stop.pk})
        self.assertEqual(client.get(f"/api/route-stops/{self.other_route_stop.pk}/").status_code, 404)

    def test_motoristas_e_funcionarios_escopados_por_empresa(self):
        client = self.client_manager()
        driver_names = {d["name"] for d in client.get("/api/drivers/").json()["results"]}
        self.assertEqual(driver_names, {"Motorista Um"})
        employee_names = {e["name"] for e in client.get("/api/employees/").json()["results"]}
        self.assertIn("Fulano", employee_names)
        self.assertNotIn("Funcionaria Beta", employee_names)

    def test_passageiros_e_eventos_escopados_por_empresa(self):
        client = self.client_manager()
        self.assertEqual(client.get(f"/api/route-passengers/{self.other_passenger.pk}/").status_code, 404)
        event = RouteEvent.objects.create(
            route=self.other_route, event_type="stop", latitude=Decimal("1"), longitude=Decimal("1")
        )
        self.assertEqual(client.get(f"/api/route-events/{event.pk}/").status_code, 404)
        r = client.post("/api/route-events/", self.event_payload(self.other_route), format="json")
        self.assertEqual(r.status_code, 403, r.content)

    def test_gestor_nao_cria_veiculo_ou_horario_em_outra_empresa(self):
        client = self.client_manager()
        r = client.post(
            "/api/vehicles/",
            {"company": self.other_company.pk, "plate_number": "ZZZ-9999", "model": "Van", "capacity": 10},
            format="json",
        )
        self.assertEqual(r.status_code, 403, r.content)
        r = client.post(
            "/api/schedules/",
            {"company": self.other_company.pk, "weekdays": [1], "time": "10:00"},
            format="json",
        )
        self.assertEqual(r.status_code, 403, r.content)


class GerentePermissionTests(RoleTestCase):
    """Gerente: cadastra empresas e pessoas, mas fica fora dos fluxos operacionais do gestor."""

    OPERATIONAL_URLS = ("/api/routes/", "/api/vehicles/", "/api/schedules/", "/api/addresses/",
                         "/api/meeting-points/", "/api/route-stops/", "/api/drivers/",
                         "/api/employees/", "/api/route-events/", "/api/route-passengers/")

    def test_gerente_faz_crud_de_empresa(self):
        client = self.client_for(self.gerente_person)
        self.assertEqual(client.get("/api/companies/").status_code, 200)
        r = client.post("/api/companies/", {"name": "Gama"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        cid = r.json()["id"]
        self.assertEqual(
            client.patch(f"/api/companies/{cid}/", {"name": "Gama 2"}, format="json").status_code, 200
        )
        self.assertEqual(client.delete(f"/api/companies/{cid}/").status_code, 204)

    def test_gerente_cadastra_pessoa_em_qualquer_empresa(self):
        client = self.client_for(self.gerente_person)
        r = client.post(
            "/api/people/",
            {"company": self.other_company.pk, "name": "Novo Gestor", "phone": "92988887777",
             "type": "EMPLOYEE", "employee_code": "E999"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.content)

    def test_gerente_nao_acessa_fluxos_operacionais_do_gestor(self):
        client = self.client_for(self.gerente_person)
        for url in self.OPERATIONAL_URLS:
            self.assertEqual(client.get(url).status_code, 403, url)
        self.assertEqual(client.get("/api/routes/my-routes/").status_code, 403)

    def test_gerente_lista_gestores_por_role(self):
        client = self.client_for(self.gerente_person)
        names = {p["name"] for p in client.get("/api/people/?role=MANAGER").json()["results"]}
        self.assertEqual(names, {"Maria Gestora"})

    def test_gerente_descobre_se_empresa_ja_tem_gestor(self):
        client = self.client_for(self.gerente_person)
        r = client.get(f"/api/people/?company_id={self.company.pk}&role=MANAGER")
        self.assertEqual([p["name"] for p in r.json()["results"]], ["Maria Gestora"])
        r = client.get(f"/api/people/?company_id={self.other_company.pk}&role=MANAGER")
        self.assertEqual(r.json()["results"], [])


class NonManagerPermissionTests(RoleTestCase):
    CRUD_URLS = ("/api/companies/", "/api/people/", "/api/drivers/", "/api/employees/",
                 "/api/routes/", "/api/route-stops/", "/api/schedules/", "/api/addresses/",
                 "/api/meeting-points/", "/api/vehicles/")

    def test_funcionario_nao_acessa_o_cadastro(self):
        client = self.client_for(self.employee_person)
        for url in self.CRUD_URLS + ("/api/route-events/", "/api/route-passengers/"):
            self.assertEqual(client.get(url).status_code, 403, url)

    def test_motorista_nao_acessa_o_cadastro_administrativo(self):
        client = self.client_for(self.driver_person)
        for url in self.CRUD_URLS:
            self.assertEqual(client.get(url).status_code, 403, url)
        self.assertEqual(
            client.post("/api/companies/", {"name": "X"}, format="json").status_code, 403
        )

    def test_funcionario_ainda_troca_a_propria_senha(self):
        client = self.client_for(self.employee_person)
        r = client.post(
            "/api/auth/change-password/",
            {"current_password": "E0010000", "new_password": "Nova-Senha-2026!"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)

    def test_funcionario_nao_usa_o_app_do_motorista(self):
        client = self.client_for(self.employee_person)
        self.assertEqual(client.get("/api/routes/my-routes/").status_code, 403)
        self.assertEqual(client.get(f"/api/routes/{self.route.pk}/events/").status_code, 403)
        self.assertEqual(
            client.post("/api/route-events/", self.event_payload(self.route), format="json").status_code,
            403,
        )


class DriverScopeTests(RoleTestCase):
    """O app do motorista (Kotlin) segue usando os mesmos endpoints, agora com checagem de dono."""

    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.driver_person)

    def test_my_routes_traz_so_as_do_motorista(self):
        r = self.client.get("/api/routes/my-routes/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([x["id"] for x in r.json()], [self.route.pk])

    def test_cria_evento_na_propria_rota(self):
        r = self.client.post(
            "/api/route-events/", self.event_payload(self.route), format="json"
        )
        self.assertEqual(r.status_code, 201, r.content)

    def test_nao_cria_evento_em_rota_de_outro_motorista(self):
        r = self.client.post(
            "/api/route-events/", self.event_payload(self.other_route), format="json"
        )
        self.assertEqual(r.status_code, 403, r.content)
        self.assertFalse(RouteEvent.objects.filter(route=self.other_route).exists())

    def test_nao_cria_evento_em_rota_sem_veiculo(self):
        route = Route.objects.create(company=self.company, name="Sem veiculo")
        r = self.client.post("/api/route-events/", self.event_payload(route), format="json")
        self.assertEqual(r.status_code, 403)

    def test_lista_so_eventos_das_proprias_rotas(self):
        RouteEvent.objects.create(route=self.route, event_type="stop", latitude=Decimal("1"), longitude=Decimal("1"))
        theirs = RouteEvent.objects.create(
            route=self.other_route, event_type="stop", latitude=Decimal("1"), longitude=Decimal("1")
        )
        results = self.client.get("/api/route-events/").json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["route"], self.route.pk)
        self.assertEqual(self.client.get(f"/api/route-events/{theirs.pk}/").status_code, 404)

    def test_nao_edita_nem_apaga_eventos(self):
        event = RouteEvent.objects.create(
            route=self.route, event_type="stop", latitude=Decimal("1"), longitude=Decimal("1")
        )
        url = f"/api/route-events/{event.pk}/"
        self.assertEqual(self.client.patch(url, {"description": "x"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)

    def test_events_da_rota(self):
        self.assertEqual(self.client.get(f"/api/routes/{self.route.pk}/events/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/routes/{self.other_route.pk}/events/").status_code, 404)

    def test_marca_embarque_do_proprio_passageiro(self):
        r = self.client.patch(
            f"/api/route-passengers/{self.passenger.pk}/",
            {"boarding_status": "confirmed"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.passenger.refresh_from_db()
        self.assertEqual(self.passenger.boarding_status, "confirmed")

    def test_nao_marca_embarque_de_passageiro_de_outra_rota(self):
        r = self.client.patch(
            f"/api/route-passengers/{self.other_passenger.pk}/",
            {"boarding_status": "confirmed"},
            format="json",
        )
        self.assertEqual(r.status_code, 404)
        self.other_passenger.refresh_from_db()
        self.assertEqual(self.other_passenger.boarding_status, "pending")

    def test_so_pode_alterar_boarding_status(self):
        url = f"/api/route-passengers/{self.passenger.pk}/"
        r = self.client.patch(
            url, {"boarding_status": "absent", "route": self.other_route.pk}, format="json"
        )
        self.assertEqual(r.status_code, 403)
        self.passenger.refresh_from_db()
        self.assertEqual(self.passenger.route_id, self.route.pk)
        self.assertEqual(self.passenger.boarding_status, "pending")

    def test_nao_usa_put_post_nem_delete_em_passageiros(self):
        url = f"/api/route-passengers/{self.passenger.pk}/"
        self.assertEqual(self.client.put(url, {"boarding_status": "absent"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/route-passengers/",
                {"person_id": self.employee_person.pk, "route": self.route.pk},
                format="json",
            ).status_code,
            403,
        )

    def test_gestor_continua_editando_passageiros_sem_restricao(self):
        client = self.client_for(self.manager_person)
        r = client.patch(
            f"/api/route-passengers/{self.passenger.pk}/",
            {"boarding_status": "absent"},
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)


class AccessRoleActionTests(RoleTestCase):
    """Promover/rebaixar para MANAGER agora é exclusivo do gerente."""

    def url(self, person):
        return f"/api/people/{person.pk}/access-role/"

    def test_gerente_promove_funcionario(self):
        client = self.client_for(self.gerente_person)
        r = client.post(self.url(self.employee_person), {"role": "MANAGER"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["role"], "MANAGER")
        self.employee_person.access_account.refresh_from_db()
        self.assertEqual(self.employee_person.access_account.role, AccessAccount.Role.MANAGER)

    def test_promovido_passa_a_acessar_o_cadastro_de_pessoas(self):
        self.client_for(self.gerente_person).post(
            self.url(self.employee_person), {"role": "MANAGER"}, format="json"
        )
        new_manager = self.client_for(Person.objects.get(pk=self.employee_person.pk))
        self.assertEqual(new_manager.get("/api/people/").status_code, 200)
        # mas continua sem acesso ao cadastro de empresas (exclusivo do gerente)
        self.assertEqual(new_manager.get("/api/companies/").status_code, 403)

    def test_gerente_rebaixa_gestor(self):
        other = self.employee(self.company, "Outro Gestor", "M002")
        AccessAccount.objects.filter(person=other).update(role="MANAGER")
        client = self.client_for(self.gerente_person)
        r = client.post(self.url(other), {"role": "EMPLOYEE"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["role"], "EMPLOYEE")

    def test_motorista_nao_vira_gestor(self):
        client = self.client_for(self.gerente_person)
        r = client.post(self.url(self.driver_person), {"role": "MANAGER"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_driver_nao_e_um_valor_aceito(self):
        client = self.client_for(self.gerente_person)
        r = client.post(self.url(self.employee_person), {"role": "DRIVER"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_pessoa_sem_conta(self):
        person = self.employee(self.company, "Sem conta", "E-X")
        AccessAccount.objects.filter(person=person).delete()
        person = Person.objects.get(pk=person.pk)
        r = self.client_for(self.gerente_person).post(
            self.url(person), {"role": "MANAGER"}, format="json"
        )
        self.assertEqual(r.status_code, 400)

    def test_gestor_nao_promove_ninguem(self):
        # O gestor perdeu o acesso a esta ação: agora é exclusiva do gerente.
        client = self.client_for(self.manager_person)
        r = client.post(self.url(self.employee_person), {"role": "MANAGER"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_nao_gerente_nao_altera_perfis(self):
        for person in (self.employee_person, self.driver_person, self.manager_person):
            r = self.client_for(person).post(
                self.url(self.employee_person), {"role": "MANAGER"}, format="json"
            )
            self.assertEqual(r.status_code, 403)


class SetAccessRoleCommandTests(RoleTestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("set_access_role", *args, stdout=out)
        return out.getvalue()

    def test_promove_por_email(self):
        email = self.employee_person.access_account.email
        self.assertIn("EMPLOYEE -> MANAGER", self.run_command(email.upper(), "MANAGER"))
        self.employee_person.access_account.refresh_from_db()
        self.assertEqual(self.employee_person.access_account.role, AccessAccount.Role.MANAGER)

    def test_email_inexistente(self):
        with self.assertRaises(CommandError):
            self.run_command("ninguem@acme.com", "MANAGER")

    def test_motorista_nao_pode_ser_promovido(self):
        with self.assertRaises(CommandError):
            self.run_command(self.driver_person.access_account.email, "MANAGER")

    def test_promove_para_gerente(self):
        email = self.employee_person.access_account.email
        self.assertIn("EMPLOYEE -> GERENTE", self.run_command(email, "GERENTE"))
        self.employee_person.access_account.refresh_from_db()
        self.assertEqual(self.employee_person.access_account.role, AccessAccount.Role.GERENTE)


class BackfillMigrationTests(TestCase):
    def test_backfill_marca_motoristas(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module("core.migrations.0006_access_account_role")

        class _Editor:
            from django.db import connection

        with override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]):
            company = Company.objects.create(name="Acme", email_domain="acme.com")
            driver = Person.objects.create(
                company=company, name="D", phone="92999990000",
                type=Person.PersonType.DRIVER, license_number="D9",
            )
        AccessAccount.objects.filter(person=driver).update(role="EMPLOYEE")  # estado pré-migração

        migration.backfill_roles(apps, _Editor())

        self.assertEqual(AccessAccount.objects.get(person=driver).role, "DRIVER")


class AdminSiteTests(RoleTestCase):
    """Django admin (Jazzmin): telas de todos os modelos, ações, perfis e idioma só no /admin."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from django.contrib.auth.models import User

        cls.superuser = User.objects.create_superuser("root", "root@example.com", "x")
        cls.schedule = Schedule.objects.create(company=cls.company, weekdays=["MON", "FRI"], time="07:30")
        cls.address = Address.objects.create(
            company=cls.company, street="Rua A", number="1", city="Manaus", state="AM",
            zip_code="69000-000", latitude="-3.1", longitude="-60.0",
        )
        cls.meeting_point = MeetingPoint.objects.create(company=cls.company, name="Ponto A", address=cls.address)
        cls.route_stop = RouteStop.objects.create(route=cls.route, meeting_point=cls.meeting_point, sequence=0)
        cls.event = RouteEvent.objects.create(
            route=cls.route, event_type="departure", latitude="-3.1", longitude="-60.0"
        )

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def admin_objects(self):
        return [
            self.company, self.employee_person, self.manager, self.route.vehicle, self.route,
            self.route_stop, self.passenger, self.meeting_point, self.address, self.schedule, self.event,
        ]

    def test_todos_os_modelos_do_core_estao_no_admin(self):
        from django.apps import apps
        from django.contrib import admin

        registered = {model for model in admin.site._registry if model._meta.app_label == "core"}
        self.assertEqual(registered, set(apps.get_app_config("core").get_models()))

    def test_listagem_e_edicao_de_cada_modelo_abrem(self):
        for obj in self.admin_objects():
            meta = obj._meta
            with self.subTest(model=meta.model_name):
                base = f"/admin/{meta.app_label}/{meta.model_name}/"
                self.assertEqual(self.client.get(base).status_code, 200)
                self.assertEqual(self.client.get(f"{base}{obj.pk}/change/").status_code, 200)
                self.assertEqual(self.client.get(f"{base}?q=a").status_code, 200)

    def test_inclusao_abre_exceto_conta_de_acesso(self):
        for obj in self.admin_objects():
            meta = obj._meta
            expected = 403 if meta.model_name == "accessaccount" else 200
            with self.subTest(model=meta.model_name):
                self.assertEqual(self.client.get(f"/admin/core/{meta.model_name}/add/").status_code, expected)

    def test_admin_em_portugues_e_api_continua_em_ingles(self):
        response = self.client.get("/admin/core/company/")
        self.assertEqual(response["Content-Language"], "pt-br")
        self.assertContains(response, "empresas", status_code=200)

        api = APIClient().get("/api/companies/")
        self.assertEqual(api.status_code, 401)
        self.assertEqual(api.json()["detail"], "Authentication credentials were not provided.")

    def test_acao_desbloquear_conta(self):
        from django.utils import timezone

        AccessAccount.objects.filter(pk=self.manager.pk).update(
            failed_login_attempts=5, blocked_until=timezone.now() + timezone.timedelta(minutes=30)
        )
        self.client.post("/admin/core/accessaccount/", {"action": "unlock", "_selected_action": [self.manager.pk]})
        self.manager.refresh_from_db()
        self.assertEqual(self.manager.failed_login_attempts, 0)
        self.assertIsNone(self.manager.blocked_until)

    def test_acao_redefinir_senha_inicial(self):
        from core.access_passwords import generate_initial_access_password

        self.manager.set_password("outra-senha")
        self.manager.must_change_password = False
        self.manager.save()
        self.client.post(
            "/admin/core/accessaccount/",
            {"action": "reset_to_initial_password", "_selected_action": [self.manager.pk]},
        )
        self.manager.refresh_from_db()
        self.assertTrue(self.manager.check_password(generate_initial_access_password(self.manager_person)))
        self.assertTrue(self.manager.must_change_password)

    def test_perfil_de_motorista_continua_validado_no_admin(self):
        url = f"/admin/core/accessaccount/{self.driver_person.access_account.pk}/change/"
        response = self.client.post(url, {"role": "MANAGER", "must_change_password": "on", "is_active": "on"})
        self.assertEqual(response.status_code, 200)  # volta ao formulário com erro
        self.driver_person.access_account.refresh_from_db()
        self.assertEqual(self.driver_person.access_account.role, AccessAccount.Role.DRIVER)


class AdminProfileGroupTests(RoleTestCase):
    """Grupos "Administrador" e "Somente leitura" criados no migrate (core.admin_profiles)."""

    def staff_in(self, group_name):
        from django.contrib.auth.models import Group, User

        user = User.objects.create_user(f"staff-{group_name}", password="x", is_staff=True)
        user.groups.add(Group.objects.get(name=group_name))
        self.client.force_login(user)
        return user

    def test_grupos_existem_com_permissoes_do_core(self):
        from django.contrib.auth.models import Group

        admin_perms = set(Group.objects.get(name="Administrador").permissions.values_list("codename", flat=True))
        self.assertIn("change_route", admin_perms)
        self.assertIn("delete_company", admin_perms)
        self.assertNotIn("change_user", admin_perms)  # não pode se promover a superusuário

        read_only = set(Group.objects.get(name="Somente leitura").permissions.values_list("codename", flat=True))
        self.assertTrue(read_only)
        self.assertTrue(all(code.startswith("view_") for code in read_only))

    def test_administrador_edita_cadastros_mas_nao_usuarios(self):
        self.staff_in("Administrador")
        self.assertEqual(self.client.get("/admin/core/route/add/").status_code, 200)
        self.assertEqual(self.client.get("/admin/auth/user/").status_code, 403)

    def test_somente_leitura_ve_mas_nao_altera(self):
        self.staff_in("Somente leitura")
        self.assertEqual(self.client.get("/admin/core/route/").status_code, 200)
        self.assertEqual(self.client.get("/admin/core/route/add/").status_code, 403)
        response = self.client.post(f"/admin/core/route/{self.route.pk}/change/", {"name": "Hack"})
        self.assertEqual(response.status_code, 403)
        self.route.refresh_from_db()
        self.assertEqual(self.route.name, "Rota A")
