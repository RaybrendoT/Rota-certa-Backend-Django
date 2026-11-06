"""Relatórios do período: frequência, rotas, motoristas e ocorrências (+ CSV)."""
from datetime import time, timedelta

from core.models import Route, RoutePassenger, Schedule
from core.tests_dashboard import THURSDAY, DashboardTestCase, at

MONDAY = THURSDAY - timedelta(days=3)
PARAMS = {"from": MONDAY.isoformat(), "to": THURSDAY.isoformat()}  # seg a qui: Rota A roda seg e qui


class ReportsTestCase(DashboardTestCase):
    def get(self, path, **extra):
        r = self.client_for(self.manager_person).get(f"/api/reports/{path}/", {**PARAMS, **extra})
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()


class AttendanceReportTests(ReportsTestCase):
    def test_frequencia_por_colaborador(self):
        second = self.employee(self.company, "Beltrano", "E002")
        rp2 = RoutePassenger.objects.create(person=second, route=self.route)
        self.board(self.passenger, "confirmed", at(MONDAY, 5, 0))
        self.board(self.passenger, "absent", at(THURSDAY, 5, 0))
        self.board(rp2, "confirmed", at(THURSDAY, 5, 0))

        data = self.get("attendance")
        self.assertEqual(data["days"], 4)
        rows = {r["name"]: r for r in data["rows"]}
        self.assertEqual(
            {k: rows["Fulano"][k] for k in ("expected", "confirmed", "absent", "no_record", "presence_rate")},
            {"expected": 2, "confirmed": 1, "absent": 1, "no_record": 0, "presence_rate": 0.5},
        )
        self.assertEqual((rows["Beltrano"]["confirmed"], rows["Beltrano"]["no_record"]), (1, 1))
        self.assertEqual(data["rows"][0]["name"], "Fulano")  # maior ausência primeiro
        self.assertEqual(data["totals"]["expected"], 4)

    def test_linha_do_tempo_do_colaborador(self):
        self.board(self.passenger, "absent", at(THURSDAY, 5, 0))
        r = self.client_for(self.manager_person).get(f"/api/reports/attendance/{self.employee_person.pk}/", PARAMS)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(
            [(d["date"], d["route_name"], d["status"]) for d in r.json()["timeline"]],
            [(MONDAY.isoformat(), "Rota A", "no_record"), (THURSDAY.isoformat(), "Rota A", "absent")],
        )
        other = self.client_for(self.manager_person).get(f"/api/reports/attendance/{self.other_driver_person.pk}/")
        self.assertEqual(other.status_code, 404)


class RoutesAndDriversReportTests(ReportsTestCase):
    def test_operacao_por_rota(self):
        self.event(self.route, "departure", at(MONDAY, 5, 0))
        self.event(self.route, "arrival", at(MONDAY, 6, 4))     # 4 min depois: no horário
        self.event(self.route, "departure", at(THURSDAY, 5, 10))
        self.event(self.route, "delay", at(THURSDAY, 5, 40))
        self.event(self.route, "arrival", at(THURSDAY, 6, 30))  # 30 min: atrasada
        self.board(self.passenger, "confirmed", at(MONDAY, 5, 5))

        [row] = self.get("routes")["rows"]
        self.assertEqual(
            {k: row[k] for k in ("planned", "done", "on_time", "late", "punctuality_rate", "avg_late_minutes", "delays")},
            {"planned": 2, "done": 2, "on_time": 1, "late": 1, "punctuality_rate": 0.5, "avg_late_minutes": 17.0,
             "delays": 1},
        )
        self.assertEqual(row["avg_occupancy"], 0.05)  # 1 embarque em 10 lugares, depois 0: média 5%

        [driver] = self.get("drivers")["rows"]
        self.assertEqual((driver["driver"], driver["vehicle"], driver["done"]), ("Motorista Um", "AAA-0001", 2))
        self.assertEqual(driver["avg_late_minutes"], 17.0)

    def test_filtros_de_turno_rota_e_motorista(self):
        afternoon = Schedule.objects.create(company=self.company, time=time(14, 0), weekdays=["MON"])
        Route.objects.create(company=self.company, name="Tarde", schedule=afternoon)
        self.assertEqual(len(self.get("routes")["rows"]), 2)
        self.assertEqual([r["name"] for r in self.get("routes", schedule=afternoon.id)["rows"]], ["Tarde"])
        self.assertEqual([r["name"] for r in self.get("routes", route=self.route.id)["rows"]], ["Rota A"])
        self.assertEqual([r["name"] for r in self.get("routes", driver=self.driver_person.id)["rows"]], ["Rota A"])


class EventsReportTests(ReportsTestCase):
    def test_ocorrencias_e_filtro_por_tipo(self):
        self.event(self.route, "delay", at(MONDAY, 5, 40))
        self.event(self.route, "incident", at(THURSDAY, 5, 50))
        self.event(self.route, "departure", at(THURSDAY, 5, 0))  # não é ocorrência
        self.event(self.other_route, "incident", at(THURSDAY, 5, 50))  # outra empresa

        data = self.get("events")
        self.assertEqual([(r["type"], r["time"], r["driver"]) for r in data["rows"]],
                         [("incident", "05:50", "Motorista Um"), ("delay", "05:40", "Motorista Um")])
        self.assertEqual([r["type"] for r in self.get("events", type="delay")["rows"]], ["delay"])


class ReportPeriodAndExportTests(ReportsTestCase):
    def test_periodo_invalido_e_permissao(self):
        client = self.client_for(self.manager_person)
        self.assertEqual(client.get("/api/reports/routes/", {"from": "2026-11-05", "to": "2026-11-01"}).status_code, 400)
        self.assertEqual(client.get("/api/reports/routes/", {"from": "2026-01-01", "to": "2026-11-01"}).status_code, 400)
        self.assertEqual(client.get("/api/reports/routes/").status_code, 200)  # padrão: últimos 30 dias
        self.assertEqual(self.client_for(self.driver_person).get("/api/reports/routes/").status_code, 403)
        self.assertEqual(self.client_for(self.gerente_person).get("/api/reports/attendance/").status_code, 403)

    def test_exporta_csv(self):
        self.board(self.passenger, "confirmed", at(MONDAY, 5, 0))
        r = self.client_for(self.manager_person).get("/api/reports/attendance/", {**PARAMS, "export": "csv"})
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"rotacerta-attendance-{MONDAY.isoformat()}-a-{THURSDAY.isoformat()}.csv", r["Content-Disposition"])
        lines = r.content.decode("utf-8").lstrip("﻿").strip().splitlines()
        self.assertEqual(lines[0].split(";")[:3], ["Colaborador", "Matrícula", "Rotas"])
        self.assertEqual(lines[1].split(";"), ["Fulano", "E001", "Rota A", "2", "1", "0", "1", "50,0%", "0,0%"])
        for kind in ("routes", "drivers", "events"):
            resp = self.client_for(self.manager_person).get(f"/api/reports/{kind}/", {**PARAMS, "export": "csv"})
            self.assertEqual(resp.status_code, 200, kind)
            self.assertTrue(resp.content.decode("utf-8").startswith("﻿"))


class ReportOptionsTests(ReportsTestCase):
    def test_opcoes_dos_filtros_so_da_empresa(self):
        r = self.client_for(self.manager_person).get("/api/reports/options/")
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual([s["time"] for s in data["schedules"]], ["06:00"])
        self.assertEqual([x["name"] for x in data["routes"]], ["Rota A"])
        self.assertEqual([d["name"] for d in data["drivers"]], ["Motorista Um"])
        self.assertEqual(data["max_days"], 90)
