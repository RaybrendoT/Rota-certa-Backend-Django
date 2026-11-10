"""Filtros da tela de Funcionários: tipo de acesso, status (ativo/inativo) e busca por nome ou matrícula."""
from core.models import Person
from core.tests import RoleTestCase


class PeopleFilterTests(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.inactive = cls.employee(cls.company, "Beltrano Inativo", "E099")
        Person.objects.filter(pk=cls.inactive.pk).update(is_active=False)

    def names(self, query=""):
        response = self.client_for(self.manager_person).get(f"/api/people/?page_size=100&{query}")
        self.assertEqual(response.status_code, 200, response.content)
        return {p["name"] for p in response.json()["results"]}

    def test_padrao_continua_so_com_ativos(self):
        names = self.names()
        self.assertIn("Fulano", names)
        self.assertNotIn("Beltrano Inativo", names)

    def test_status_inativo_e_todos(self):
        self.assertEqual(self.names("status=inactive"), {"Beltrano Inativo"})
        all_names = self.names("status=all")
        self.assertTrue({"Fulano", "Beltrano Inativo"} <= all_names)

    def test_tipo_de_acesso(self):
        self.assertEqual(self.names("access=DRIVER"), {"Motorista Um"})
        self.assertEqual(self.names("access=MANAGER"), {"Maria Gestora"})
        self.assertEqual(self.names("access=GERENTE"), {"Gustavo Gerente"})
        self.assertEqual(self.names("access=EMPLOYEE"), {"Fulano"})
        self.assertEqual(self.names("access=EMPLOYEE&status=all"), {"Fulano", "Beltrano Inativo"})

    def test_busca_por_nome_e_matricula_sem_maiusculas(self):
        self.assertEqual(self.names("search=fulano"), {"Fulano"})
        self.assertEqual(self.names("search=e099&status=all"), {"Beltrano Inativo"})
        self.assertEqual(self.names("search=d001"), {"Motorista Um"})

    def test_inativo_abre_e_pode_ser_reativado(self):
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get(f"/api/people/{self.inactive.pk}/").status_code, 200)
        response = client.patch(f"/api/people/{self.inactive.pk}/", {"is_active": True}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("Beltrano Inativo", self.names())

    def test_gestor_nao_ve_inativos_de_outra_empresa(self):
        other = self.employee(self.other_company, "Inativo Beta", "B099")
        Person.objects.filter(pk=other.pk).update(is_active=False)
        self.assertNotIn("Inativo Beta", self.names("status=all"))
