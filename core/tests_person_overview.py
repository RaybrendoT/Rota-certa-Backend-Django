"""Painel lateral do cadastro de pessoas: GET people/{id}/overview/."""
from datetime import time, timedelta
from decimal import Decimal

from core.dates import local_today
from core.models import Address, Person, Route, RoutePassenger, Schedule
from core.tests import RoleTestCase


class PersonOverviewTests(RoleTestCase):
    def url(self, person):
        return f"/api/people/{person.pk}/overview/"

    def test_funcionario_com_conta_endereco_e_rotas(self):
        Address.objects.create(
            person=self.employee_person, street="Rua A", number="10", city="Manaus", state="AM",
            zip_code="69000-000", latitude=Decimal("-3.1"), longitude=Decimal("-60.0"),
        )
        morning = Schedule.objects.create(company=self.company, time=time(6, 0), weekdays=["MON", "TUE"])
        self.route.schedule = morning
        self.route.save(update_fields=["schedule"])
        tomorrow = local_today() + timedelta(days=1)
        extra = Route.objects.create(company=self.company, name="Extra", vehicle=self.route.vehicle, service_date=tomorrow)
        RoutePassenger.objects.create(person=self.employee_person, route=extra)
        past = Route.objects.create(
            company=self.company, name="Passada", vehicle=self.route.vehicle, service_date=local_today() - timedelta(days=1)
        )
        RoutePassenger.objects.create(person=self.employee_person, route=past)

        r = self.client_for(self.manager_person).get(self.url(self.employee_person))
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data["account"]["email"], self.employee_person.access_account.email)
        self.assertEqual(data["addresses"][0]["street"], "Rua A")
        self.assertEqual(
            [(x["name"], x["as"], x["arrival"], x["service_date"]) for x in data["routes"]],
            [("Rota A", "passenger", "06:00", None), ("Extra", "passenger", None, tomorrow.isoformat())],
        )

    def test_motorista_ve_as_rotas_que_dirige(self):
        data = self.client_for(self.manager_person).get(self.url(self.driver_person)).json()
        self.assertEqual([(x["name"], x["as"], x["vehicle"]) for x in data["routes"]], [("Rota A", "driver", "Van AAA-0001")])

    def test_mesmo_nome_e_telefone_aparece_como_outro_vinculo(self):
        twin = Person.objects.create(
            company=self.company, name="fulano", phone=self.employee_person.phone,
            type=Person.PersonType.DRIVER, license_number="D099",
        )
        Person.objects.create(  # mesmo nome, outra empresa: não conta
            company=self.other_company, name="Fulano", phone=self.employee_person.phone,
            type=Person.PersonType.DRIVER, license_number="D098",
        )
        data = self.client_for(self.manager_person).get(self.url(self.employee_person)).json()
        self.assertEqual(data["linked_records"], [
            {"id": twin.id, "name": "fulano", "type": "DRIVER", "is_active": True, "code": "D099"},
        ])

    def test_escopo_e_permissao(self):
        self.assertEqual(self.client_for(self.manager_person).get(self.url(self.other_driver_person)).status_code, 404)
        self.assertEqual(self.client_for(self.employee_person).get(self.url(self.driver_person)).status_code, 403)
