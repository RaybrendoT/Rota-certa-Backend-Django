"""Embarque por dia (BoardingRecord): gravado quando motorista ou colaborador muda o status."""
from datetime import timedelta
from unittest import mock

from core.dates import local_today
from core.models import BoardingRecord
from core.tests import RoleTestCase


class BoardingRecordTests(RoleTestCase):
    def patch_as_driver(self, status):
        return self.client_for(self.driver_person).patch(
            f"/api/route-passengers/{self.passenger.pk}/", {"boarding_status": status}, format="json"
        )

    def test_motorista_marca_e_grava_o_dia(self):
        self.assertEqual(self.patch_as_driver("confirmed").status_code, 200)
        record = BoardingRecord.objects.get()
        self.assertEqual(
            (record.service_date, record.status, record.marked_by_id, record.route_id, record.person_id),
            (local_today(), "confirmed", self.driver_person.pk, self.route.pk, self.employee_person.pk),
        )

    def test_mudar_de_novo_no_mesmo_dia_atualiza_o_registro(self):
        self.patch_as_driver("confirmed")
        self.patch_as_driver("absent")
        self.assertEqual(list(BoardingRecord.objects.values_list("status", flat=True)), ["absent"])

    def test_outro_dia_vira_outro_registro(self):
        self.patch_as_driver("confirmed")
        with mock.patch("core.boarding.local_today", return_value=local_today() + timedelta(days=1)):
            self.patch_as_driver("absent")
        self.assertEqual(
            list(BoardingRecord.objects.order_by("service_date").values_list("status", flat=True)),
            ["confirmed", "absent"],
        )

    def test_colaborador_marca_pelo_app(self):
        r = self.client_for(self.employee_person).patch(
            f"/api/me/routes/{self.passenger.pk}/", {"boarding_status": "absent"}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.content)
        record = BoardingRecord.objects.get()
        self.assertEqual((record.status, record.marked_by_id), ("absent", self.employee_person.pk))

    def test_mesmo_status_nao_grava(self):
        self.patch_as_driver("pending")
        self.assertFalse(BoardingRecord.objects.exists())
