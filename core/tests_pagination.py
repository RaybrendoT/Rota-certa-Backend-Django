"""Paginação: o seletor de itens por página (?page_size=) é respeitado, com teto."""
from core.models import Vehicle
from core.pagination import StandardPagination
from core.tests import RoleTestCase


class PageSizeTests(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Vehicle.objects.bulk_create(
            Vehicle(company=cls.company, plate_number=f"TST-{n:04d}", model="Van", capacity=10) for n in range(30)
        )

    def test_page_size_do_painel(self):
        client = self.client_for(self.manager_person)
        default = client.get("/api/vehicles/").json()
        self.assertEqual((default["count"], len(default["results"])), (31, 10))
        for size in (5, 25, 50):
            data = client.get("/api/vehicles/", {"page_size": size, "page": 1}).json()
            self.assertEqual(len(data["results"]), min(size, 31), size)
        second = client.get("/api/vehicles/", {"page_size": 25, "page": 2}).json()
        self.assertEqual(len(second["results"]), 6)

    def test_teto_do_page_size(self):
        self.assertEqual(StandardPagination.max_page_size, 500)
        data = self.client_for(self.manager_person).get("/api/vehicles/", {"page_size": 100000}).json()
        self.assertEqual(len(data["results"]), 31)  # aceito até o teto, sem erro
