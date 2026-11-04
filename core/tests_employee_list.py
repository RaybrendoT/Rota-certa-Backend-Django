"""Lista de colaboradores anexada ao planejamento (CSV / Excel com matrícula e nome)."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from core.planning.employee_list import EmployeeListError, find_columns, match_employee_list, read_table
from core.tests import RoleTestCase


def xlsx_bytes(rows):
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class EmployeeListBase(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ana = cls.employee(cls.company, "Ana Paula Souza", "1001")
        cls.bruno = cls.employee(cls.company, "Bruno Lima", "1002")
        cls.carla = cls.employee(cls.company, "Carla Mendes", "1003")
        cls.joao = cls.employee(cls.company, "João Silva", "1004")
        cls.joana = cls.employee(cls.company, "João Silveira", "1005")
        cls.outra = cls.employee(cls.other_company, "Diana Outra", "2001")


class ReadTableTests(EmployeeListBase):
    def test_csv_com_ponto_e_virgula_e_acentos_do_excel(self):
        data = "Matrícula;Nome\n1001;Ana Paula Souza\n\n1002;Bruno Lima\n".encode("cp1252")
        rows = read_table("lista.csv", data)
        self.assertEqual(rows, [["Matrícula", "Nome"], ["1001", "Ana Paula Souza"], ["1002", "Bruno Lima"]])
        self.assertEqual(find_columns(rows), (0, 1, True))

    def test_xlsx_com_matricula_numerica(self):
        rows = read_table("lista.xlsx", xlsx_bytes([["Nome do colaborador", "Matr."], ["Ana", 1001.0]]))
        self.assertEqual(rows[1], ["Ana", "1001"])
        self.assertEqual(find_columns(rows), (1, 0, True))

    def test_sem_cabecalho_a_coluna_com_digitos_e_a_matricula(self):
        rows = read_table("lista.csv", b"Ana Paula Souza,1001\nBruno Lima,1002\n")
        self.assertEqual(find_columns(rows), (1, 0, False))

    def test_formatos_recusados(self):
        with self.assertRaisesMessage(EmployeeListError, ".xls antigo"):
            read_table("lista.xls", b"\xd0\xcf\x11\xe0resto")
        with self.assertRaisesMessage(EmployeeListError, ".csv ou .xlsx"):
            read_table("lista.pdf", b"%PDF")
        with self.assertRaisesMessage(EmployeeListError, "vazio"):
            read_table("lista.csv", b";\n;\n")
        with self.assertRaisesMessage(EmployeeListError, "Excel"):
            read_table("lista.xlsx", b"PK nada de zip")


class MatchTests(EmployeeListBase):
    def match(self, text, name="lista.csv"):
        return match_employee_list(self.company, name, text.encode())

    def test_confere_matricula_e_nome(self):
        result = self.match(
            "matricula;nome\n"
            "1001;Ana Paula Souza\n"
            "1002;Carla Errada\n"      # matrícula vale, nome não bate
            "9999;Carla Mendes\n"      # matrícula inexistente, acha pelo nome
            "9998;Fulano de Tal\n"     # não existe
            ";João\n"                   # dois Joãos
            "1001;Ana P. Souza\n"      # repetida
            "2001;Diana Outra\n"       # outra empresa
        )
        self.assertEqual(result["rows"], 7)
        matched = {m["person"]["id"]: m for m in result["matched"]}
        self.assertEqual(set(matched), {self.ana.id, self.bruno.id, self.carla.id})
        self.assertEqual(matched[self.ana.id]["matched_by"], "code")
        self.assertFalse(matched[self.ana.id]["name_differs"])
        self.assertTrue(matched[self.bruno.id]["name_differs"])
        self.assertEqual(matched[self.carla.id]["matched_by"], "name")
        self.assertEqual(matched[self.carla.id]["row"], 4)
        self.assertEqual([a["name"] for a in result["ambiguous"]], ["João"])
        self.assertEqual(len(result["ambiguous"][0]["candidates"]), 2)
        self.assertEqual([n["code"] for n in result["not_found"]], ["9998", "2001"])
        self.assertEqual(result["duplicates"], 1)

    def test_coluna_unica(self):
        result = self.match("1003\nBruno Lima\n7777\n")
        self.assertEqual([m["person"]["id"] for m in result["matched"]], [self.carla.id, self.bruno.id])
        self.assertEqual([n["code"] for n in result["not_found"]], ["7777"])

    def test_limite_de_linhas(self):
        with self.assertRaisesMessage(EmployeeListError, "mais de 1000"):
            self.match("\n".join(str(n) for n in range(1002)))


class EmployeeListApiTests(EmployeeListBase):
    URL = "/api/planning/employee-list/"

    def upload(self, person, name, data):
        return self.client_for(person).post(self.URL, {"file": SimpleUploadedFile(name, data)}, format="multipart")

    def test_gestor_envia_xlsx(self):
        r = self.upload(self.manager_person, "turno.xlsx", xlsx_bytes([["Matrícula", "Nome"], [1003, "Carla Mendes"]]))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["file_name"], "turno.xlsx")
        self.assertEqual(r.json()["matched"][0]["person"]["code"], "1003")

    def test_erros(self):
        self.assertEqual(self.client_for(self.manager_person).post(self.URL, {}, format="multipart").status_code, 400)
        r = self.upload(self.manager_person, "velho.xls", b"\xd0\xcf\x11\xe0")
        self.assertEqual(r.status_code, 400)
        self.assertIn(".xlsx", r.json()["detail"])

    def test_so_gestor(self):
        self.assertEqual(self.upload(self.driver_person, "l.csv", b"1001").status_code, 403)
        self.assertEqual(self.upload(self.employee_person, "l.csv", b"1001").status_code, 403)
