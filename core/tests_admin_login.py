"""Login do admin com o template próprio (templates/admin/login.html), no visual do painel."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.tests import FAST_HASHERS


@FAST_HASHERS
class AdminLoginTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "senha-de-teste-123"
        cls.admin = get_user_model().objects.create_superuser("admin-teste", "admin@teste.local", cls.password)

    def test_usa_o_template_no_visual_do_painel(self):
        response = self.client.get("/admin/login/?next=/admin/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/login.html")
        self.assertContains(response, "Entrar no admin")
        self.assertContains(response, 'name="next" value="/admin/"')

    def test_credenciais_validas_entram_no_admin(self):
        response = self.client.post(
            "/admin/login/", {"username": "admin-teste", "password": self.password, "next": "/admin/"}
        )
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_credenciais_erradas_mostram_erro_e_mantem_o_usuario(self):
        response = self.client.post("/admin/login/", {"username": "admin-teste", "password": "errada", "next": "/admin/"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="auth-error"')
        self.assertContains(response, 'value="admin-teste"')
