import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from core import eta
from core.models import Address, EtaSample, Notification, RoutePassenger, StopArrival, VehicleLocation
from core.tests import RoleTestCase
from core.tests_realtime import make_stop

LAT = -3.1
# Paradas a cada 0,01° de longitude (~1,1 km) e a empresa no fim. O colaborador embarca na 3ª.
LNGS = [-60.00, -60.01, -60.02, -60.03]
COMPANY_LNG = -60.04


class FakeGoogleResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@override_settings(GOOGLE_MAPS_API_KEY="", BUS_APPROACHING_MINUTES=5)
class EtaTestCase(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.stops = [make_stop(cls.route, f"Ponto {i}", i, Decimal(str(LAT)), Decimal(str(lng)))
                     for i, lng in enumerate(LNGS)]
        Address.objects.create(company=cls.company, street="Empresa", number="1", city="Manaus", state="AM",
                               zip_code="69000000", latitude=Decimal(str(LAT)), longitude=Decimal(str(COMPANY_LNG)))
        cls.passenger.route_stop = cls.stops[2]
        cls.passenger.save()

    def setUp(self):
        super().setUp()
        patcher = mock.patch("core.realtime.send_to_group")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.driver = self.client_for(self.driver_person)
        self.url = f"/api/routes/{self.route.pk}/location/"

    def drive(self, lng, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.driver.post(self.url, {"latitude": LAT, "longitude": lng, **extra}, format="json")
        self.assertEqual(r.status_code, 202, r.content)
        return r

    def estimate(self):
        """Previsão com o passageiro lido de novo do banco (posição atual, não a guardada em memória)."""
        return eta.estimate(RoutePassenger.objects.select_related("route").get(pk=self.passenger.pk))

    def approaching(self):
        return Notification.objects.filter(recipient=self.employee_person, kind=Notification.Kind.BUS_APPROACHING)


class EstimateTests(EtaTestCase):
    def test_previsao_pelo_trajeto_com_paradas_antes(self):
        self.drive(-60.005)  # entre a 1ª e a 2ª parada; o ponto do colaborador é a 3ª (~1,67 km)
        est = self.estimate()
        self.assertEqual(est["phase"], eta.Phase.APPROACHING)
        self.assertTrue(est["is_live"])
        self.assertAlmostEqual(est["distance_meters"], 1667, delta=15)
        self.assertEqual(est["stops_before"], 1)
        # 1,67 km a 25 km/h (240 s) + 60 s parado na 2ª parada
        self.assertAlmostEqual(est["seconds"], 300, delta=5)
        self.assertEqual(est["minutes"], 5)

    def test_velocidade_do_gps_maior_que_a_media(self):
        self.drive(-60.015, speed=20)  # 72 km/h
        self.assertEqual(self.estimate()["minutes"], 1)

    def test_chegando_passou_e_destino(self):
        self.drive(-60.018)
        self.assertEqual(self.estimate()["phase"], eta.Phase.ARRIVING)
        self.drive(-60.025)
        self.assertEqual(self.estimate()["phase"], eta.Phase.PASSED)
        self.drive(-60.0395)
        self.assertEqual(self.estimate()["phase"], eta.Phase.AT_DESTINATION)

    def test_posicao_antiga_nao_conta_como_ao_vivo(self):
        self.drive(-60.005)
        VehicleLocation.objects.filter(route=self.route).update(recorded_at=timezone.now() - timedelta(minutes=10))
        est = self.estimate()
        self.assertFalse(est["is_live"])
        self.assertEqual(est["phase"], eta.Phase.WAITING)

    def test_progresso_continua_quando_o_trajeto_se_cruza(self):
        self.drive(-60.005)
        progress = VehicleLocation.objects.get(route=self.route).progress_meters
        self.assertAlmostEqual(progress, 555, delta=10)

    def test_colaborador_recebe_previsao_e_trajeto(self):
        self.drive(-60.005)
        client = self.client_for(self.employee_person)
        [item] = client.get("/api/me/routes/").json()
        self.assertEqual(item["eta"]["minutes"], 5)
        self.assertEqual(item["path"]["source"], "straight")
        self.assertTrue(item["path"]["polyline"])
        r = client.get(f"/api/me/routes/{self.passenger.pk}/eta/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["eta"]["stops_before"], 1)
        self.assertEqual(r.json()["last_location"]["longitude"], -60.005)

    def test_eta_de_outro_colaborador_nao_aparece(self):
        r = self.client_for(self.employee_person).get(f"/api/me/routes/{self.other_passenger.pk}/eta/")
        self.assertEqual(r.status_code, 404)


class ApproachingNotificationTests(EtaTestCase):
    def test_avisa_pela_previsao_uma_vez(self):
        self.drive(-60.0)  # ~2,2 km e 1 parada antes: ~6 min, acima do limite de 5
        self.assertFalse(self.approaching().exists())
        self.drive(-60.012)  # ~0,9 km: ~3 min
        self.drive(-60.013)
        [notification] = self.approaching()
        self.assertIn("Ponto 2", notification.message)
        self.assertIn("min", notification.message)
        self.assertEqual(notification.data["eta_minutes"], 3)

    def test_nao_avisa_no_ponto(self):
        self.drive(-60.0195)
        self.assertFalse(self.approaching().exists())


class FieldTestRecordTests(EtaTestCase):
    def test_registra_previsoes_e_chegada_e_gera_relatorio(self):
        start = timezone.now()
        with mock.patch("core.notifications.timezone.now", return_value=start):
            self.drive(-60.0)
        for i, lng in enumerate([-60.006, -60.012, -60.0199], start=1):
            later = start + timedelta(minutes=i)
            with mock.patch("django.utils.timezone.now", return_value=later):
                self.drive(lng)
        self.assertEqual(EtaSample.objects.filter(passenger=self.passenger).count(), 3)
        [arrival] = StopArrival.objects.filter(passenger=self.passenger)
        self.assertEqual(arrival.service_date, EtaSample.objects.first().service_date)

        out, err = io.StringIO(), io.StringIO()
        call_command("relatorio_eta", data=arrival.service_date.isoformat(), stdout=out, stderr=err)
        lines = out.getvalue().strip().splitlines()
        self.assertTrue(lines[0].startswith("data,rota,passageiro,ponto"))
        self.assertEqual(len(lines), 4)  # cabeçalho + 3 previsões
        self.assertIn("Erro absoluto médio", err.getvalue())


@override_settings(GOOGLE_MAPS_API_KEY="test-key")
class RoadPathTests(EtaTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def fake_google(self, *args, **kwargs):
        from core.road_path import encode_polyline

        legs = []
        all_lngs = LNGS + [COMPANY_LNG]
        for a, b in zip(all_lngs, all_lngs[1:]):  # cada trecho contorna o quarteirão
            legs.append({"distanceMeters": 2220, "polyline": {
                "encodedPolyline": encode_polyline([(LAT, a), (LAT + 0.005, a), (LAT + 0.005, b), (LAT, b)])}})
        return FakeGoogleResponse({"routes": [{"distanceMeters": 2220 * 4, "legs": legs}]})

    def test_trajeto_pelas_ruas_calculado_uma_vez(self):
        with mock.patch("core.road_path.urllib.request.urlopen", side_effect=self.fake_google) as api:
            client = self.client_for(self.employee_person)
            [item] = client.get("/api/me/routes/").json()
            client.get("/api/me/routes/")
            self.drive(-60.0)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(item["path"]["source"], "google")
        self.route.refresh_from_db()
        self.assertEqual(self.route.road_path["stop_indexes"], [0, 3, 6, 9, 12])
        # Pelo contorno, até a 3ª parada são 2 trechos de ~2,2 km (e não 2,2 km em linha reta).
        self.assertAlmostEqual(self.estimate()["distance_meters"], 4440, delta=60)

    def test_sem_chave_usa_linha_reta(self):
        with override_settings(GOOGLE_MAPS_API_KEY=""), \
                mock.patch("core.road_path.urllib.request.urlopen") as api:
            [item] = self.client_for(self.employee_person).get("/api/me/routes/").json()
        api.assert_not_called()
        self.assertEqual(item["path"]["source"], "straight")


class SimulatorTests(EtaTestCase):
    def test_simulador_percorre_a_rota_e_registra_eventos(self):
        with mock.patch("core.management.commands.simular_rota.time.sleep"), \
                self.captureOnCommitCallbacks(execute=True):
            call_command("simular_rota", route_id=self.route.pk, minutos=1, interval=5, stdout=io.StringIO())
        location = VehicleLocation.objects.get(route=self.route)
        self.assertAlmostEqual(float(location.longitude), COMPANY_LNG, places=4)
        self.assertEqual(list(self.route.events.order_by("timestamp").values_list("event_type", flat=True)),
                         ["departure", "arrival"])
        self.assertTrue(StopArrival.objects.filter(passenger=self.passenger).exists())
