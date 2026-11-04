"""Testes da navegação do motorista (GET /routes/{id}/navigation/), com a Routes API simulada."""
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from core.models import Address
from core.tests import RoleTestCase
from core.tests_realtime import make_stop


def lat_lng(latitude, longitude):
    return {"latLng": {"latitude": latitude, "longitude": longitude}}


def google_payload(leg_count):
    legs = [
        {
            "distanceMeters": 1000 * (index + 1),
            "duration": f"{120 * (index + 1)}s",
            "steps": [
                {
                    "distanceMeters": 400,
                    "startLocation": lat_lng(-3.0, -60.0),
                    "endLocation": lat_lng(-3.01, -60.0),
                    "navigationInstruction": {"maneuver": "TURN_RIGHT", "instructions": f"Vire à direita {index}"},
                },
                {
                    "distanceMeters": 600,
                    "startLocation": lat_lng(-3.01, -60.0),
                    "endLocation": lat_lng(-3.02, -60.0),
                    "navigationInstruction": {},
                },
            ],
        }
        for index in range(leg_count)
    ]
    return {
        "routes": [
            {"duration": "600s", "distanceMeters": 5000, "polyline": {"encodedPolyline": "abc"}, "legs": legs}
        ]
    }


@override_settings(GOOGLE_MAPS_API_KEY="test-key")
class DriverNavigationTests(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.first_stop = make_stop(cls.route, "Ponto Um", 0, Decimal("-3.100000"), Decimal("-60.000000"))
        cls.second_stop = make_stop(cls.route, "Ponto Dois", 1, Decimal("-3.200000"), Decimal("-60.100000"))
        # Destino: endereço da empresa que não é ponto de embarque.
        Address.objects.create(
            company=cls.company, street="Av. Torquato Tapajós", number="1", city="Manaus",
            state="AM", zip_code="69000000", latitude=Decimal("-3.050000"), longitude=Decimal("-59.950000"),
        )
        Address.objects.create(
            person=cls.driver_person, street="Rua do Motorista", number="9", city="Manaus",
            state="AM", zip_code="69000000", latitude=Decimal("-3.130000"), longitude=Decimal("-60.020000"),
        )

    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.driver_person)
        patcher = mock.patch("core.viewsets._compute_google_route")
        self.google = patcher.start()
        self.addCleanup(patcher.stop)

    def navigation(self, query=""):
        return self.client.get(f"/api/routes/{self.route.pk}/navigation/{query}")

    def test_sem_origem_parte_do_endereco_do_motorista(self):
        self.google.return_value = google_payload(3)
        r = self.navigation()
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["origin_source"], "home")
        self.assertEqual(body["waypoints"]["origin"], {"latitude": -3.13, "longitude": -60.02})
        self.assertEqual(self.google.call_args.kwargs["origin"], {"latitude": -3.13, "longitude": -60.02})
        self.assertEqual(len(self.google.call_args.kwargs["intermediates"]), 2)
        self.assertEqual(body["destination_name"], "Acme")
        self.assertEqual(
            [(s["id"], s["title"], s["done"]) for s in body["stops"]],
            [(self.first_stop.pk, "Ponto Um", False), (self.second_stop.pk, "Ponto Dois", False)],
        )
        # Campos que o app antigo já usava continuam iguais.
        self.assertEqual(body["instruction"], "Vire à direita 0")
        self.assertEqual(body["duration_text"], "10 min")
        self.assertEqual(body["distance_text"], "5.0 km")

    def test_trechos_e_passos(self):
        self.google.return_value = google_payload(3)
        body = self.navigation().json()
        self.assertEqual(
            body["legs"],
            [
                {"stop_id": self.first_stop.pk, "distance_meters": 1000, "duration_seconds": 120},
                {"stop_id": self.second_stop.pk, "distance_meters": 2000, "duration_seconds": 240},
                {"stop_id": None, "distance_meters": 3000, "duration_seconds": 360},
            ],
        )
        self.assertEqual(len(body["steps"]), 6)
        first, second = body["steps"][:2]
        self.assertEqual(first["leg_index"], 0)
        self.assertEqual(first["maneuver"], "TURN_RIGHT")
        self.assertEqual(first["distance_meters"], 400)
        self.assertEqual(first["end"], {"latitude": -3.01, "longitude": -60.0})
        self.assertEqual(second["instruction"], "Siga em frente")
        self.assertEqual(body["steps"][-1]["leg_index"], 2)

    def test_origem_gps_e_paradas_feitas(self):
        self.google.return_value = google_payload(2)
        r = self.navigation(f"?origin=-3.11,-60.01&done_stops={self.first_stop.pk}")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["origin_source"], "gps")
        kwargs = self.google.call_args.kwargs
        self.assertEqual(kwargs["origin"], {"latitude": -3.11, "longitude": -60.01})
        self.assertEqual(kwargs["intermediates"], [{"latitude": -3.2, "longitude": -60.1}])
        self.assertEqual([s["done"] for s in body["stops"]], [True, False])
        self.assertEqual([leg["stop_id"] for leg in body["legs"]], [self.second_stop.pk, None])

    def test_todas_as_paradas_feitas_vai_direto_ao_destino(self):
        self.google.return_value = google_payload(1)
        done = f"{self.first_stop.pk},{self.second_stop.pk}"
        body = self.navigation(f"?origin=-3.11,-60.01&done_stops={done}").json()
        self.assertEqual(self.google.call_args.kwargs["intermediates"], [])
        self.assertEqual(body["legs"][0]["stop_id"], None)

    def test_origem_invalida(self):
        for value in ["abc", "-3.1", "-3.1,-60,1", "200,10"]:
            with self.subTest(value=value):
                self.assertEqual(self.navigation(f"?origin={value}").status_code, 400)
        self.google.assert_not_called()

    def test_done_stops_ignora_lixo(self):
        self.google.return_value = google_payload(3)
        r = self.navigation("?done_stops=x,,abc")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(self.google.call_args.kwargs["intermediates"]), 2)

    def test_motorista_de_outra_rota_nao_acessa(self):
        r = self.client_for(self.other_driver_person).get(f"/api/routes/{self.route.pk}/navigation/")
        self.assertEqual(r.status_code, 404)

    @override_settings(GOOGLE_MAPS_API_KEY="")
    def test_sem_chave_do_google(self):
        self.assertEqual(self.navigation().status_code, 503)
