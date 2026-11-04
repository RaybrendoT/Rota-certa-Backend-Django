from django.test import TestCase
from rest_framework.test import APIClient


class HealthCheckTests(TestCase):
    """GET /api/health/ é o que o deploy automático confere depois de subir a versão nova."""

    def test_health_is_public_and_checks_the_database(self):
        res = APIClient().get("/api/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"status": "ok", "database": True})
