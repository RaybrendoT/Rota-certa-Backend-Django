"""Criação e edição manual de rotas (stepper do painel): prévia, gravação, edição e API."""
from datetime import time, timedelta

from core import route_builder
from core.dates import local_today
from core.models import AccessAccount, Notification, Route, RoutePassenger, RouteStop, Schedule, Vehicle
from core.tests_planning import POINTS, PlanningBase, address


class BuilderBase(PlanningBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # 09:00 fica a 3h da rota fixa das 06:00 do motorista ocupado; 07:00 fica perto dela.
        cls.morning = Schedule.objects.create(company=cls.company, time=time(9, 0), weekdays=["MON", "TUE", "WED", "THU", "FRI"])
        cls.early = Schedule.objects.create(company=cls.company, time=time(7, 0), weekdays=["MON", "TUE", "WED", "THU", "FRI"])
        cls.bus = Vehicle.objects.get(plate_number="ACM-0010")
        cls.busy_van = Vehicle.objects.get(plate_number="ACM-0011")
        cls.north = [p for p in cls.riders if "Norte" in p.name]
        cls.south = [p for p in cls.riders if "Sul" in p.name]

    def data(self, people, **extra):
        return {
            "name": "Rota manual", "kind": "fixed", "schedule": self.morning.id, "service_date": None,
            "vehicle": self.bus.id, "passengers": [p.id for p in people], "stops": None, "optimize": False,
            **extra,
        }


class PreviewTests(BuilderBase):
    def test_paradas_no_ponto_mais_perto_e_horarios(self):
        result = route_builder.preview(self.company, self.data(self.north[:3] + self.south[:2]))
        self.assertTrue(result["can_save"], result["warnings"])
        self.assertEqual(result["load"], 5)
        stops = {s["meeting_point"]["short_name"]: [p["id"] for p in s["passengers"]] for s in result["stops"]}
        self.assertEqual(set(stops), {"Norte", "Sul"})
        self.assertEqual(sorted(stops["Norte"]), sorted(p.id for p in self.north[:3]))
        etas = [s["eta"] for s in result["stops"]]
        self.assertEqual(etas, sorted(etas))  # a ordem respeita o trajeto até a empresa
        self.assertLess(etas[-1], "09:00")
        self.assertEqual(result["arrival"], "09:00")
        self.assertEqual(result["driver"]["name"], "Motorista Dois Acme")

    def test_lotacao_motorista_ocupado_e_sem_endereco_sao_erros(self):
        result = route_builder.preview(
            self.company, self.data(self.riders[:8] + [self.joana], vehicle=self.busy_van.id, schedule=self.early.id)
        )
        codes = {w["code"] for w in result["warnings"] if w["level"] == "error"}
        self.assertEqual(codes, {"capacity", "driver_busy", "without_home"})
        self.assertFalse(result["can_save"])

    def test_embarque_longe_de_casa_e_aviso(self):
        far = self.employee(self.company, "Pessoa Longe", "L01")
        address(POINTS["Norte"][0] + 0.02, POINTS["Norte"][1], person=far)  # ~2,2 km do ponto Norte
        result = route_builder.preview(self.company, self.data([far]))
        [warning] = [w for w in result["warnings"] if w["code"] == "far_pickup"]
        self.assertEqual(warning["level"], "warning")
        self.assertIn("Pessoa Longe", warning["message"])
        self.assertTrue(result["can_save"])

    def test_ordem_manual_e_mantida_e_otimizar_reordena(self):
        auto = route_builder.preview(self.company, self.data(self.north[:2] + self.south[:2]))
        reversed_stops = [
            {"meeting_point": s["meeting_point"]["id"], "passengers": [p["id"] for p in s["passengers"]]}
            for s in reversed(auto["stops"])
        ]
        manual = route_builder.preview(self.company, self.data(self.north[:2] + self.south[:2], stops=reversed_stops))
        self.assertEqual([s["meeting_point"]["id"] for s in manual["stops"]], [s["meeting_point"] for s in reversed_stops])
        optimized = route_builder.preview(
            self.company, self.data(self.north[:2] + self.south[:2], stops=reversed_stops, optimize=True)
        )
        self.assertEqual([s["meeting_point"]["id"] for s in optimized["stops"]], [s["meeting_point"]["id"] for s in auto["stops"]])

    def test_pessoa_movida_para_outra_parada_fica_la(self):
        north_point, south_point = self.points["Norte"], self.points["Sul"]
        stops = [
            {"meeting_point": north_point.id, "passengers": [self.north[0].id]},
            {"meeting_point": south_point.id, "passengers": [self.south[0].id, self.north[1].id]},
        ]
        result = route_builder.preview(self.company, self.data([self.north[0], self.north[1], self.south[0]], stops=stops))
        south = next(s for s in result["stops"] if s["meeting_point"]["id"] == south_point.id)
        self.assertIn(self.north[1].id, [p["id"] for p in south["passengers"]])

    def test_ja_vai_em_outra_rota_no_mesmo_horario(self):
        route, _ = route_builder.save(self.company, self.data(self.north[:2]))
        other = Vehicle.objects.create(company=self.company, driver=self.driver(self.company, "Motorista Três", "D012"),
                                       plate_number="ACM-0012", model="Ônibus", capacity=20)
        result = route_builder.preview(self.company, self.data(self.north[:1], vehicle=other.id))
        [warning] = [w for w in result["warnings"] if w["code"] == "double_booked"]
        self.assertIn(route.name, warning["message"])

    def test_avulsa_precisa_de_data_futura(self):
        past = (local_today() - timedelta(days=1)).isoformat()
        result = route_builder.preview(self.company, self.data(self.north[:1], kind="extra", service_date=past))
        self.assertIn("service_date", {w["code"] for w in result["warnings"] if w["level"] == "error"})

    def test_outra_empresa_nao_entra(self):
        with self.assertRaises(route_builder.BuilderError):
            route_builder.preview(self.company, self.data([self.foreigner]))


class SaveTests(BuilderBase):
    def test_cria_rota_fixa_com_paradas_e_passageiros_e_avisa(self):
        route, result = route_builder.save(self.company, self.data(self.north[:3] + self.south[:2]))
        self.assertEqual(route.schedule, self.morning)
        self.assertIsNone(route.service_date)
        self.assertEqual(route.stops.count(), 2)
        self.assertEqual(RoutePassenger.objects.filter(route=route, is_active=True).count(), 5)
        self.assertFalse(RoutePassenger.objects.filter(route=route, route_stop__isnull=True).exists())
        assigned = Notification.objects.filter(route=route, kind=Notification.Kind.ROUTE_ASSIGNED)
        self.assertEqual(assigned.count(), 6)  # 5 passageiros + o motorista
        self.assertEqual(result["load"], 5)

    def test_avulsa_grava_a_data(self):
        day = (local_today() + timedelta(days=2)).isoformat()
        route, _ = route_builder.save(self.company, self.data(self.north[:2], kind="extra", service_date=day))
        self.assertEqual(route.service_date.isoformat(), day)

    def test_nao_salva_com_erro_nem_sem_nome(self):
        with self.assertRaises(route_builder.BuilderInvalid):
            route_builder.save(self.company, self.data(self.riders[:11]))  # 11 pessoas, 10 lugares
        with self.assertRaises(route_builder.BuilderInvalid):
            route_builder.save(self.company, self.data(self.north[:1], name="  "))
        self.assertFalse(Route.objects.filter(name="Rota manual").exists())

    def test_edicao_reaproveita_paradas_desativa_quem_saiu_e_avisa_quem_mudou(self):
        route, _ = route_builder.save(self.company, self.data(self.north[:3] + self.south[:2]))
        north_stop = route.stops.get(meeting_point=self.points["Norte"])
        Notification.objects.all().delete()

        loaded = route_builder.load(route)["data"]
        stops = loaded["stops"]
        north = next(s for s in stops if s["meeting_point"] == self.points["Norte"].id)
        south = next(s for s in stops if s["meeting_point"] == self.points["Sul"].id)
        moved = north["passengers"].pop()
        south["passengers"].append(moved)
        removed = south["passengers"].pop(0)
        passengers = [pid for s in stops for pid in s["passengers"]]
        route_builder.save(self.company, {**loaded, "passengers": passengers, "stops": stops, "name": "Rota editada"}, route)

        route.refresh_from_db()
        self.assertEqual(route.name, "Rota editada")
        self.assertTrue(RouteStop.objects.filter(pk=north_stop.pk).exists())
        self.assertFalse(RoutePassenger.objects.get(route=route, person_id=removed).is_active)
        self.assertEqual(RoutePassenger.objects.get(route=route, person_id=moved).route_stop.meeting_point, self.points["Sul"])
        kinds = dict(Notification.objects.values_list("recipient_id", "kind"))
        self.assertEqual(kinds[moved], Notification.Kind.PICKUP_CHANGED)
        self.assertEqual(kinds[removed], Notification.Kind.ROUTE_UPDATED)

    def test_quem_volta_para_a_rota_e_reativado(self):
        route, _ = route_builder.save(self.company, self.data(self.north[:2]))
        route_builder.save(self.company, self.data(self.north[:1]), route)
        route_builder.save(self.company, self.data(self.north[:2]), route)
        self.assertEqual(RoutePassenger.objects.filter(route=route, is_active=True).count(), 2)
        self.assertEqual(RoutePassenger.objects.filter(route=route).count(), 2)


class ApiTests(BuilderBase):
    def test_fluxo_pela_api(self):
        client = self.client_for(self.manager_person)
        options = client.get("/api/route-builder/options/").json()
        self.assertIn(self.morning.id, [s["id"] for s in options["schedules"]])
        rider = next(e for e in options["employees"] if e["id"] == self.north[0].id)
        self.assertEqual(rider["nearest_point"]["id"], self.points["Norte"].id)

        vehicles = client.get(f"/api/route-builder/vehicles/?schedule={self.early.id}").json()["vehicles"]
        busy = next(v for v in vehicles if v["id"] == self.busy_van.id)
        self.assertIn("Fixa 06h", busy["conflict"])

        preview = client.post("/api/route-builder/preview/", self.data(self.north[:2]), format="json")
        self.assertEqual(preview.status_code, 200, preview.content)
        created = client.post("/api/route-builder/", self.data(self.north[:2]), format="json")
        self.assertEqual(created.status_code, 201, created.content)
        route_id = created.json()["id"]

        loaded = client.get(f"/api/route-builder/{route_id}/").json()
        self.assertEqual(sorted(loaded["data"]["passengers"]), sorted(p.id for p in self.north[:2]))
        updated = client.put(f"/api/route-builder/{route_id}/", {**loaded["data"], "name": "Nova"}, format="json")
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(Route.objects.get(pk=route_id).name, "Nova")

    def test_erro_de_regra_volta_400_com_a_previa(self):
        client = self.client_for(self.manager_person)
        r = client.post("/api/route-builder/", self.data(self.riders[:11]), format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("capacity", {w["code"] for w in r.json()["preview"]["warnings"]})

    def test_so_gestor_e_so_a_propria_empresa(self):
        self.assertEqual(self.client_for(self.driver_person).get("/api/route-builder/options/").status_code, 403)
        route, _ = route_builder.save(self.company, self.data(self.north[:1]))
        other = self.employee(self.other_company, "Gestor Beta", "MB01")
        account = other.access_account
        account.role = AccessAccount.Role.MANAGER
        account.save(update_fields=["role", "updated_at"])
        self.assertEqual(self.client_for(other).get(f"/api/route-builder/{route.id}/").status_code, 404)
