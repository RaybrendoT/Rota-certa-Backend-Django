"""Push pelo Firebase Cloud Messaging, com o Google simulado."""
import io
import json
import urllib.error
import urllib.parse
from unittest import mock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import override_settings

from core import push
from core.models import Notification, PushDevice
from core.tests_realtime import RealtimeTestCase

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ACCOUNT = {
    "type": "service_account", "project_id": "rota-certa-test", "private_key_id": "kid1",
    "private_key": _KEY.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ).decode(),
    "client_email": "fcm@rota-certa-test.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}
TOKEN = "fcm-token-aparelho-1"
SEND_URL = "https://fcm.googleapis.com/v1/projects/rota-certa-test/messages:send"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@override_settings(FCM_SERVICE_ACCOUNT_JSON=json.dumps(ACCOUNT), FCM_SERVICE_ACCOUNT_FILE="", PUSH_ASYNC=False)
class PushTests(RealtimeTestCase):
    def setUp(self):
        super().setUp()
        push._cache.update(account=None, account_key=None, token=None, expires_at=0.0)
        self.messages, self.oauth = [], []
        self.fcm_error = None
        patcher = mock.patch("core.push.urllib.request.urlopen", side_effect=self.fake_google)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_google(self, request, *args, **kwargs):
        if request.full_url == ACCOUNT["token_uri"]:
            self.oauth.append(urllib.parse.parse_qs(request.data.decode()))
            return FakeResponse({"access_token": "ya29.test", "expires_in": 3599})
        self.assertEqual(request.full_url, SEND_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer ya29.test")
        if self.fcm_error:
            raise self.fcm_error()
        self.messages.append(json.loads(request.data)["message"])
        return FakeResponse({"name": f"projects/rota-certa-test/messages/{len(self.messages)}"})

    def register(self, person, token=TOKEN):
        return self.client_for(person).post("/api/me/devices/", {"token": token}, format="json")

    def depart(self):
        client = self.client_for(self.driver_person)
        with self.captureOnCommitCallbacks(execute=True):
            r = client.post("/api/route-events/", self.event_payload(self.route, event_type="departure"), format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_registrar_e_remover_aparelho(self):
        client = self.client_for(self.employee_person)
        self.assertEqual(client.get("/api/me/devices/").data, {"push_enabled": True, "devices": 0})
        self.assertEqual(self.register(self.employee_person).status_code, 201)
        self.assertEqual(self.register(self.employee_person).status_code, 200)  # mesmo token: só renova
        self.assertEqual(client.post("/api/me/devices/", {}, format="json").status_code, 400)
        r = client.post("/api/me/devices/unregister/", {"token": TOKEN}, format="json")
        self.assertEqual(r.status_code, 204)
        self.assertFalse(PushDevice.objects.get().is_active)

    def test_token_passa_para_quem_registrou_por_ultimo(self):
        self.register(self.employee_person)
        self.register(self.driver_person)
        self.assertEqual(PushDevice.objects.get().person, self.driver_person)

    def test_saida_do_onibus_vai_por_push(self):
        self.register(self.employee_person)
        self.depart()
        [message] = self.messages
        notification = Notification.objects.get(recipient=self.employee_person)
        self.assertEqual(message["token"], TOKEN)
        self.assertNotIn("notification", message)  # só data: o app monta a notificação
        self.assertEqual(message["data"], {
            "notification_id": str(notification.pk), "kind": "route_departure", "route_id": str(self.route.pk),
            "title": "Seu ônibus saiu", "message": "A rota Rota A foi iniciada.",
        })
        self.assertEqual(message["android"], {"priority": "HIGH"})
        claims = jwt.decode(self.oauth[0]["assertion"][0], _KEY.public_key(), algorithms=["RS256"],
                            audience=ACCOUNT["token_uri"])
        self.assertEqual(claims["scope"], push.FCM_SCOPE)

    def test_sem_aparelho_ou_sem_firebase_nao_envia(self):
        self.depart()
        self.assertEqual(self.messages, [])
        self.register(self.employee_person)
        with override_settings(FCM_SERVICE_ACCOUNT_JSON=""):
            self.depart()
        self.assertEqual(self.messages, [])
        self.assertEqual(Notification.objects.filter(recipient=self.employee_person).count(), 2)

    def test_token_invalido_desativa_o_aparelho(self):
        body = (b'{"error": {"code": 404, "status": "NOT_FOUND", "message": "not found", "details":'
                b' [{"@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", "errorCode": "UNREGISTERED"}]}}')
        self.fcm_error = lambda: urllib.error.HTTPError(SEND_URL, 404, "Not Found", {}, io.BytesIO(body))
        self.register(self.employee_person)
        self.depart()
        device = PushDevice.objects.get()
        self.assertFalse(device.is_active)
        self.assertIn("UNREGISTERED", device.deactivated_reason)
        self.assertTrue(Notification.objects.filter(recipient=self.employee_person).exists())
