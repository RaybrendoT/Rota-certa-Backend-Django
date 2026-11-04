"""Endereço residencial atualizado pelo próprio colaborador no app (GPS ou digitado)."""
from decimal import Decimal
from unittest import mock

from core.models import Address, MeetingPoint, Notification
from core.tests import RoleTestCase

DEPOT = (-3.1135, -59.9675)
CENTRO = (-3.1300, -60.0200)
NORTE = (-3.0300, -59.9800)
HOME = (-3.1305, -60.0205)  # ~80 m do ponto Centro
GEOCODED = {
    "street": "Rua das Flores", "number": "123", "sublocality": "Centro", "city": "Manaus", "state": "AM",
    "postal_code": "69005-010", "formatted": "Rua das Flores, 123 - Centro, Manaus - AM",
}


def address(lat, lng, **owner):
    return Address.objects.create(
        street="Rua Teste", number="1", city="Manaus", state="AM", zip_code="69000-000",
        latitude=Decimal(f"{lat:.6f}"), longitude=Decimal(f"{lng:.6f}"), **owner,
    )


def geocoded(lat=HOME[0], lng=HOME[1], exact=True, **overrides):
    return {**GEOCODED, "latitude": lat, "longitude": lng, "exact": exact, **overrides}


@mock.patch("core.self_address.reverse_geocode", return_value=GEOCODED)
@mock.patch("core.self_address.geocode_address", return_value=None)
class SelfAddressTests(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        address(*DEPOT, company=cls.company)
        cls.centro = MeetingPoint.objects.create(company=cls.company, name="Centro – Rua A", address=address(*CENTRO, company=cls.company))
        cls.norte = MeetingPoint.objects.create(company=cls.company, name="Norte – Rua B", address=address(*NORTE, company=cls.company))

    def setUp(self):
        super().setUp()
        self.client_api = self.client_for(self.employee_person)

    def preview(self, **data):
        return self.client_api.post("/api/me/address/preview/", data, format="json")

    def save(self, **data):
        body = {
            "source": "gps", "street": "Rua das Flores", "number": "123", "complement": "Casa 2", "neighborhood": "Centro",
            "city": "Manaus", "state": "am", "postal_code": "69005010",
            "latitude": HOME[0], "longitude": HOME[1],
            "gps_latitude": HOME[0], "gps_longitude": HOME[1], "gps_accuracy": 15, "gps_is_mock": False,
            **data,
        }
        return self.client_api.put("/api/me/address/", body, format="json")

    # ------------------------------------------------------------------ prévia

    def test_previa_gps(self, geocode, reverse):
        r = self.preview(mode="gps", latitude=HOME[0], longitude=HOME[1], accuracy=20)
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data["address"]["street"], "Rua das Flores")
        self.assertEqual(data["address"]["postal_code"], "69005-010")
        self.assertEqual(data["nearest_point"]["name"], "Centro – Rua A")
        self.assertLess(data["nearest_point"]["distance_m"], 150)

    def test_gps_impreciso_simulado_ou_longe(self, geocode, reverse):
        weak = self.preview(mode="gps", latitude=HOME[0], longitude=HOME[1], accuracy=120)
        self.assertEqual(weak.status_code, 400)
        self.assertIn("GPS fraco", weak.json()["detail"])
        mock_location = self.preview(mode="gps", latitude=HOME[0], longitude=HOME[1], accuracy=5, is_mock=True)
        self.assertIn("simulada", mock_location.json()["detail"])
        far = self.preview(mode="gps", latitude=-23.55, longitude=-46.63, accuracy=10)
        self.assertEqual(far.status_code, 400)
        self.assertIn("km da empresa", far.json()["detail"])

    def test_previa_digitada(self, geocode, reverse):
        typed = {"mode": "typed", "postal_code": "69005-010", "street": "Rua das Flores", "number": "123", "city": "Manaus", "state": "AM"}
        geocode.return_value = geocoded(exact=False)
        r = self.preview(**typed)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["exact"])
        self.assertTrue(any("alfinete" in w for w in r.json()["warnings"]))

        geocode.return_value = geocoded(postal_code="69099-000")
        mismatch = self.preview(**typed)
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn("não corresponde", mismatch.json()["detail"])

        geocode.return_value = None
        self.assertIn("Não encontramos", self.preview(**typed).json()["detail"])
        self.assertEqual(self.preview(mode="typed", street="Rua").status_code, 400)  # campos obrigatórios

    # ------------------------------------------------------------------ gravação

    def test_grava_como_pendente_e_avisa_gestor(self, geocode, reverse):
        old = address(*NORTE, person=self.employee_person)
        r = self.save(latitude=HOME[0] + 0.0009)  # alfinete arrastado ~100 m
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("aprovação", r.json()["message"])
        self.assertIn("Centro – Rua A", r.json()["message"])
        self.assertEqual(r.json()["address"]["id"], old.id)  # o anterior continua valendo
        self.assertEqual(r.json()["pending"]["review_status"], "pending")

        old.refresh_from_db()
        self.assertTrue(old.is_active)
        pending = self.employee_person.addresses.get(review_status=Address.Review.PENDING)
        self.assertFalse(pending.is_active)
        self.assertEqual((pending.source, pending.precision, pending.accuracy_m), ("gps", "gps", 15))
        self.assertEqual((pending.state, pending.zip_code, pending.complement), ("AM", "69005-010", "Casa 2"))
        self.assertEqual(pending.created_by, self.employee_person)

        notice = Notification.objects.get(kind=Notification.Kind.ADDRESS_CHANGED)
        self.assertEqual(notice.recipient, self.manager_person)
        self.assertEqual(notice.title, "Novo endereço para aprovar")

    def test_novo_envio_substitui_o_pendente(self, geocode, reverse):
        self.assertEqual(self.save().status_code, 200)
        first = self.employee_person.addresses.get(review_status=Address.Review.PENDING)
        self.assertEqual(self.save(number="200").status_code, 200)
        first.refresh_from_db()
        self.assertEqual(first.review_status, Address.Review.REJECTED)
        self.assertEqual(self.employee_person.addresses.filter(review_status=Address.Review.PENDING).count(), 1)
        self.assertIsNone(self.client_api.get("/api/me/address/").json()["rejected"])  # substituído não é recusa a mostrar

    def test_alfinete_longe_demais(self, geocode, reverse):
        r = self.save(latitude=HOME[0] + 0.0045)  # ~500 m
        self.assertEqual(r.status_code, 400)
        self.assertIn("alfinete", r.json()["detail"])
        self.assertFalse(self.employee_person.addresses.exists())

    def test_digitado_confere_de_novo_ao_gravar(self, geocode, reverse):
        geocode.return_value = geocoded(exact=False)
        r = self.save(source="typed", gps_latitude=None, gps_longitude=None, gps_accuracy=None)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.employee_person.addresses.get(review_status=Address.Review.PENDING).precision, "approximate")
        geocode.return_value = None
        self.assertEqual(self.save(source="typed").status_code, 400)

    def test_limite_de_trocas_por_semana(self, geocode, reverse):
        for _ in range(3):
            address(*HOME, person=self.employee_person, source=Address.Source.TYPED, is_active=False)
        r = self.save()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.client_api.get("/api/me/address/").json()["changes_left_this_week"], 0)

    def test_consulta_atual(self, geocode, reverse):
        self.assertIsNone(self.client_api.get("/api/me/address/").json()["address"])
        address(*HOME, person=self.employee_person)
        data = self.client_api.get("/api/me/address/").json()
        self.assertEqual(data["address"]["source"], "panel")
        self.assertEqual(data["nearest_point"]["name"], "Centro – Rua A")
        self.assertEqual(data["changes_left_this_week"], 3)

    @mock.patch("core.address_views.lookup_cep")
    def test_cep(self, lookup, geocode, reverse):
        lookup.return_value = {"postal_code": "69005-010", "street": "Rua das Flores", "neighborhood": "Centro", "city": "Manaus", "state": "AM"}
        r = self.client_api.get("/api/me/address/cep/69005010/")
        self.assertEqual(r.json()["street"], "Rua das Flores")
        lookup.return_value = None
        self.assertEqual(self.client_api.get("/api/me/address/cep/00000000/").status_code, 404)

    def test_painel_nao_altera_origem(self, geocode, reverse):
        home = address(*HOME, person=self.employee_person)
        self.client_for(self.manager_person).patch(f"/api/addresses/{home.pk}/", {"source": "gps", "number": "9"}, format="json")
        home.refresh_from_db()
        self.assertEqual((home.source, home.number), ("panel", "9"))
