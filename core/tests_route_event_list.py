"""Lista de ocorrências: cada evento já traz o nome da rota."""
from decimal import Decimal

from core.models import RouteEvent
from core.tests import RoleTestCase


class RouteEventListTests(RoleTestCase):
    def test_evento_traz_o_nome_da_rota_e_filtra_por_tipo(self):
        for kind in ("delay", "incident"):
            RouteEvent.objects.create(
                route=self.route, event_type=kind, description="x", latitude=Decimal("-3.1"), longitude=Decimal("-60.0")
            )
        r = self.client_for(self.manager_person).get("/api/route-events/", {"event_type": "incident"})
        self.assertEqual(r.status_code, 200, r.content)
        items = r.json()["results"] if isinstance(r.json(), dict) else r.json()
        self.assertEqual([(e["event_type"], e["route_name"]) for e in items], [("incident", "Rota A")])
