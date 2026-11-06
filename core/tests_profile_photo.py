"""Foto do perfil: PUT/DELETE me/photo/ e o campo photo no perfil da sessão."""
from django.core.files.uploadedfile import SimpleUploadedFile

from core.profile_photo import MAX_PHOTO_BYTES
from core.tests import RoleTestCase

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


def upload(content, name="foto.jpg", content_type="image/jpeg"):
    return {"photo": SimpleUploadedFile(name, content, content_type=content_type)}


class ProfilePhotoTests(RoleTestCase):
    def test_envia_foto_e_ela_volta_no_perfil(self):
        client = self.client_for(self.manager_person)
        r = client.put("/api/me/photo/", upload(JPEG), format="multipart")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["person"]["photo"].startswith("data:image/jpeg;base64,/9j/"))
        me = client.get("/api/auth/me/").json()
        self.assertEqual(me["person"]["photo"], r.json()["person"]["photo"])

    def test_tipo_vem_do_conteudo_e_nao_do_nome(self):
        client = self.client_for(self.employee_person)
        r = client.put("/api/me/photo/", upload(PNG, name="foto.jpg"), format="multipart")
        self.assertTrue(r.json()["person"]["photo"].startswith("data:image/png;base64,"))
        bad = client.put("/api/me/photo/", upload(b"<svg onload=alert(1)>", name="x.png", content_type="image/png"),
                         format="multipart")
        self.assertEqual(bad.status_code, 400)

    def test_limite_de_tamanho_e_campo_obrigatorio(self):
        client = self.client_for(self.manager_person)
        big = JPEG + b"\x00" * MAX_PHOTO_BYTES
        self.assertEqual(client.put("/api/me/photo/", upload(big), format="multipart").status_code, 400)
        self.assertEqual(client.put("/api/me/photo/", {}, format="multipart").status_code, 400)

    def test_remove_foto(self):
        client = self.client_for(self.manager_person)
        client.put("/api/me/photo/", upload(JPEG), format="multipart")
        r = client.delete("/api/me/photo/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["person"]["photo"])
        self.manager_person.refresh_from_db()
        self.assertIsNone(self.manager_person.photo)

    def test_sem_login_nao_envia(self):
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().put("/api/me/photo/", upload(JPEG), format="multipart").status_code, 401)
