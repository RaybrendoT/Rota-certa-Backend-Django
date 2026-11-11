import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.access_passwords import generate_initial_access_password
from core.importacao_csv import ImportErrors, import_people
from core.models import AccessAccount, Address, Company, Person, Vehicle
from core.self_address import company_location
from core.tests import RoleTestCase

HEADER = "perfil,nome,matricula,cnh,telefone,email,logradouro,numero,bairro,cidade,uf,cep,latitude,longitude,placa,modelo_veiculo,capacidade\n"
SEDE = "EMPRESA,MostraTech,,,,,Avenida Buriti,1500,Distrito Industrial I,Manaus,AM,69075-000,-3.113516,-59.967525,,,\n"
GESTOR = "GESTOR,Maria Gestora,MTG001,,(92) 98871-8384,mtg001@mostratech.com.br,Rua A,10,Aleixo,Manaus,AM,69000-000,-3.10,-60.00,,,\n"
GERENTE = "GERENTE,Joao Gerente,MTR001,,(92) 98235-1707,,,,,,,,,,,,\n"
MOTORISTA = "MOTORISTA,Marcos Motorista,,63896621965,(92) 99336-6239,,,,,,,,,,VBA-9F43,Volare W9,28\n"
COLAB = "COLABORADOR,Ana Colaboradora,MT0001,,(92) 99111-2222,,Rua B,20,Cidade Nova,Manaus,AM,69000-000,-3.03,-59.98,,,\n"


def csv_bytes(*lines, sep=","):
    return (HEADER + "".join(lines)).replace(",", sep).encode("utf-8")


class ImportarCsvTests(TestCase):
    def test_importa_todos_os_perfis(self):
        result = import_people(csv_bytes(SEDE, GESTOR, GERENTE, MOTORISTA, COLAB), "MostraTech", "mostratech.com.br")
        company = Company.objects.get(name="MostraTech")
        self.assertEqual(company.email_domain, "mostratech.com.br")
        self.assertEqual(result.created, {"COLABORADOR": 1, "GESTOR": 1, "GERENTE": 1, "MOTORISTA": 1})
        roles = dict(AccessAccount.objects.filter(person__company=company).values_list("email", "role"))
        self.assertEqual(roles, {
            "mtg001@mostratech.com.br": "MANAGER", "mtr001@mostratech.com.br": "GERENTE",
            "63896621965@mostratech.com.br": "DRIVER", "mt0001@mostratech.com.br": "EMPLOYEE",
        })
        vehicle = Vehicle.objects.get(company=company)
        self.assertEqual((vehicle.plate_number, vehicle.capacity, vehicle.driver.name), ("VBA9F43", 28, "Marcos Motorista"))
        self.assertEqual(company_location(company).street, "Avenida Buriti")
        ana = Person.objects.get(employee_code="MT0001")
        self.assertEqual(ana.addresses.get().neighborhood, "Cidade Nova")
        self.assertTrue(ana.access_account.check_password(generate_initial_access_password(ana)))
        self.assertTrue(ana.access_account.must_change_password)

    def test_rodar_de_novo_nao_duplica(self):
        data = csv_bytes(SEDE, GESTOR, MOTORISTA, COLAB)
        import_people(data, "MostraTech", "mostratech.com.br")
        again = import_people(data, "MostraTech")
        self.assertEqual((again.skipped, sum(again.created.values()), again.company_address), (3, 0, False))
        self.assertEqual(Person.objects.count(), 3)
        self.assertEqual(Address.objects.filter(company__name="MostraTech", is_active=True).count(), 1)

    def test_aceita_ponto_e_virgula_do_excel(self):
        import_people(csv_bytes(SEDE, COLAB, sep=";"), "MostraTech", "mostratech.com.br")
        self.assertTrue(Person.objects.filter(employee_code="MT0001").exists())

    def test_com_erro_nada_e_gravado(self):
        sem_matricula = COLAB.replace("MT0001", "").replace("Ana", "Bia")
        email_errado = GESTOR.replace("mtg001@", "outro@")
        with self.assertRaises(ImportErrors) as ctx:
            import_people(csv_bytes(SEDE, COLAB, sem_matricula, email_errado, "VISITANTE,Zé,,,,,,,,,,,,,,,\n"),
                          "MostraTech", "mostratech.com.br")
        text = "\n".join(ctx.exception.errors)
        self.assertIn("Linha 4: matricula vazia", text)
        self.assertIn("Linha 5: e-mail 'outro@mostratech.com.br' não bate", text)
        self.assertIn("Linha 6: perfil 'VISITANTE' inválido", text)
        self.assertFalse(Company.objects.filter(name="MostraTech").exists())
        self.assertFalse(Person.objects.exists())

    def test_motorista_sem_veiculo_e_coordenada_trocada(self):
        sem_placa = MOTORISTA.replace("VBA-9F43", "")
        trocada = COLAB.replace("-3.03,-59.98", "-59.98,-3.03")
        with self.assertRaises(ImportErrors) as ctx:
            import_people(csv_bytes(sem_placa, trocada), "MostraTech", "mostratech.com.br")
        text = "\n".join(ctx.exception.errors)
        self.assertIn("motorista sem placa", text)
        self.assertIn("coordenada fora do Brasil", text)

    def test_comando_dry_run_e_csv_de_exemplo(self):
        out = io.StringIO()
        call_command("gerar_csv_exemplo", "--colaboradores", "5", "--motoristas", "2", stdout=out)
        path = self._tmp(out.getvalue())
        result = io.StringIO()
        call_command("importar_csv", path, "--empresa", "MostraTech", "--dominio", "mostratech.com.br", "--dry-run",
                     stdout=result)
        self.assertIn("5 colaboradores, 1 gestor(es), 1 gerente(s), 2 motoristas", result.getvalue())
        self.assertFalse(Person.objects.exists())
        with self.assertRaises(CommandError):
            call_command("importar_csv", "/nao/existe.csv", "--empresa", "X", stdout=io.StringIO())

    def _tmp(self, text):
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(lambda: __import__("os").unlink(handle.name))
        return handle.name


class ImportarCsvPainelTests(RoleTestCase):
    """POST /api/people/import/: prévia, confirmação e permissões."""

    URL = "/api/people/import/"

    def upload(self, client, *lines, **extra):
        data = (HEADER + "".join(lines)).encode("utf-8")
        return client.post(self.URL, {"file": SimpleUploadedFile("lista.csv", data, "text/csv"), **extra}, format="multipart")

    def acme(self, line):
        return line.replace("mostratech.com.br", "acme.com").replace("MostraTech", "Acme")

    def test_gestor_ve_a_previa_sem_gravar(self):
        r = self.upload(self.client_for(self.manager_person), self.acme(SEDE), MOTORISTA, COLAB)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertFalse(body["confirmed"])
        self.assertEqual(body["created"], {"employees": 1, "drivers": 1, "managers": 0})
        self.assertEqual((body["vehicles"], body["company"]["name"]), (1, "Acme"))
        self.assertFalse(Person.objects.filter(employee_code="MT0001").exists())

    @override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.PBKDF2PasswordHasher"])
    def test_gestor_confirma_e_grava_na_propria_empresa(self):
        r = self.upload(self.client_for(self.manager_person), COLAB, MOTORISTA, confirm="true")
        self.assertEqual(r.status_code, 201, r.content)
        ana = Person.objects.get(employee_code="MT0001")
        self.assertEqual(ana.company, self.company)
        self.assertEqual(ana.access_account.email, "mt0001@acme.com")
        self.assertTrue(ana.access_account.password_hash.startswith("pbkdf2_sha256$20000$"))
        self.assertTrue(ana.access_account.check_password(generate_initial_access_password(ana)))
        again = self.upload(self.client_for(self.manager_person), COLAB, MOTORISTA, confirm="true").json()
        self.assertEqual((again["skipped"], again["created"]["employees"]), (2, 0))

    def test_gestor_nao_cria_gestor_nem_gerente(self):
        r = self.upload(self.client_for(self.manager_person), GESTOR, GERENTE, COLAB, confirm="true")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"], [
            "Linha 2: perfil GESTOR não pode ser importado aqui.",
            "Linha 3: perfil GERENTE não pode ser importado aqui.",
        ])
        self.assertFalse(Person.objects.filter(employee_code="MT0001").exists())

    def test_erros_por_linha_e_nada_gravado(self):
        r = self.upload(self.client_for(self.manager_person), COLAB, COLAB.replace("Rua B", ""), confirm="true")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(any("Linha 3" in e for e in r.json()["errors"]))
        self.assertFalse(Person.objects.filter(employee_code="MT0001").exists())

    def test_gerente_escolhe_a_empresa_e_pode_criar_gestor(self):
        client = self.client_for(self.gerente_person)
        self.assertEqual(self.upload(client, COLAB).status_code, 400)
        r = self.upload(client, self.acme(GESTOR).replace("mtg001@acme.com", ""), company=self.other_company.pk,
                        confirm="true")
        self.assertEqual(r.status_code, 201, r.content)
        gestor = Person.objects.get(employee_code="MTG001")
        self.assertEqual((gestor.company, gestor.access_account.role), (self.other_company, "MANAGER"))

    def test_colaborador_e_motorista_nao_importam(self):
        for person in (self.employee_person, self.driver_person):
            self.assertEqual(self.upload(self.client_for(person), COLAB).status_code, 403)

    def test_sem_arquivo(self):
        r = self.client_for(self.manager_person).post(self.URL, {}, format="multipart")
        self.assertEqual(r.status_code, 400)
