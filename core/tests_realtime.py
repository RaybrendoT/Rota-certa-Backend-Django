"""Testes do app mobile em tempo real: colaborador, GPS do motorista, notificações e WebSocket."""
from datetime import time
from decimal import Decimal
from unittest import mock

from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase

from config.asgi import application
from core.models import (
    Address,
    Company,
    MeetingPoint,
    Notification,
    Person,
    RouteEvent,
    RoutePassenger,
    RouteStop,
    Schedule,
    VehicleLocation,
)
from core.realtime import person_group, route_group
from core.tests import FAST_HASHERS, RoleTestCase
from core.tokens import AccessAccountRefreshToken

STOP_LAT, STOP_LNG = Decimal("-3.100000"), Decimal("-60.000000")
# ~550 m ao norte da parada: dentro do raio de "chegando", fora do raio de "no ponto".
NEAR_LAT = -3.095
# ~110 m da parada.
AT_STOP_LAT = -3.099


def make_stop(route, name="Ponto Central", sequence=0, lat=STOP_LAT, lng=STOP_LNG):
    address = Address.objects.create(
        company=route.company, street="Av. Djalma Batista", number="100", city="Manaus",
        state="AM", zip_code="69000000", latitude=lat, longitude=lng,
    )
    point = MeetingPoint.objects.create(company=route.company, name=name, address=address)
    return RouteStop.objects.create(route=route, meeting_point=point, sequence=sequence)


class RealtimeTestCase(RoleTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.stop = make_stop(cls.route)
        cls.passenger.route_stop = cls.stop
        cls.passenger.save()
        Schedule.objects.create(company=cls.company, weekdays=["MON", "FRI"], time=time(7, 30))

    def setUp(self):
        super().setUp()
        patcher = mock.patch("core.realtime.send_to_group")
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)

    def broadcasts(self, group=None):
        return [
            payload
            for (g, payload), _ in self.sent.call_args_list
            if group is None or g == group
        ]

    def notifications_of(self, person, kind=None):
        qs = Notification.objects.filter(recipient=person)
        return qs.filter(kind=kind) if kind else qs


class CollaboratorRoutesTests(RealtimeTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.employee_person)

    def test_lista_rotas_do_colaborador(self):
        r = self.client.get("/api/me/routes/")
        self.assertEqual(r.status_code, 200, r.content)
        [item] = r.json()
        self.assertEqual(item["id"], self.passenger.pk)
        self.assertEqual(item["boarding_status"], "pending")
        self.assertEqual(item["route"]["name"], "Rota A")
        self.assertEqual(item["route"]["driver_name"], "Motorista Um")
        self.assertEqual(item["route"]["vehicle"]["plate_number"], "AAA-0001")
        self.assertEqual(item["route"]["route_status"], "waiting")
        self.assertEqual(item["stop"]["title"], "Ponto Central")
        self.assertEqual(item["stop"]["latitude"], -3.1)
        self.assertIsNone(item["last_location"])
        self.assertEqual(item["schedules"], [{"weekdays": ["MON", "FRI"], "time": "07:30"}])

    def test_gestor_que_e_passageiro_ve_so_as_proprias_rotas(self):
        r = self.client_for(self.manager_person).get("/api/me/routes/")
        self.assertEqual([x["id"] for x in r.json()], [self.other_passenger.pk])

    def test_motorista_nao_acessa(self):
        self.assertEqual(self.client_for(self.driver_person).get("/api/me/routes/").status_code, 403)

    def test_sem_token(self):
        self.client.credentials()
        self.assertEqual(self.client.get("/api/me/routes/").status_code, 401)

    def test_confirma_presenca_e_avisa_motorista(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.patch(
                f"/api/me/routes/{self.passenger.pk}/", {"boarding_status": "confirmed"}, format="json"
            )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["boarding_status"], "confirmed")
        self.passenger.refresh_from_db()
        self.assertEqual(self.passenger.boarding_status, "confirmed")

        [notification] = self.notifications_of(self.driver_person)
        self.assertEqual(notification.kind, Notification.Kind.BOARDING_CONFIRMED)
        self.assertEqual(notification.title, "Fulano confirmou embarque")
        self.assertEqual(notification.message, "Ponto: Ponto Central")

        [boarding] = [p for p in self.broadcasts(route_group(self.route.pk)) if p["type"] == "boarding_status"]
        self.assertEqual(boarding["status"], "confirmed")
        self.assertEqual(boarding["route_passenger_id"], self.passenger.pk)
        self.assertEqual((boarding["route_name"], boarding["person_name"]), ("Rota A", "Fulano"))
        [pushed] = self.broadcasts(person_group(self.driver_person.pk))
        self.assertEqual(pushed["notification"]["id"], notification.pk)

    def test_informa_ausencia(self):
        self.client.patch(f"/api/me/routes/{self.passenger.pk}/", {"boarding_status": "absent"}, format="json")
        [notification] = self.notifications_of(self.driver_person)
        self.assertEqual(notification.kind, Notification.Kind.BOARDING_ABSENT)

    def test_mesmo_status_nao_gera_aviso(self):
        r = self.client.patch(f"/api/me/routes/{self.passenger.pk}/", {"boarding_status": "pending"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.notifications_of(self.driver_person).exists())

    def test_nao_altera_vinculo_de_outra_pessoa(self):
        r = self.client.patch(
            f"/api/me/routes/{self.other_passenger.pk}/", {"boarding_status": "confirmed"}, format="json"
        )
        self.assertEqual(r.status_code, 404)

    def test_so_altera_boarding_status(self):
        r = self.client.patch(
            f"/api/me/routes/{self.passenger.pk}/",
            {"boarding_status": "confirmed", "route_stop_id": None},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.passenger.refresh_from_db()
        self.assertEqual(self.passenger.boarding_status, "pending")

    def test_status_invalido(self):
        r = self.client.patch(f"/api/me/routes/{self.passenger.pk}/", {"boarding_status": "boarded"}, format="json")
        self.assertEqual(r.status_code, 400)


class DriverLocationTests(RealtimeTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.driver_person)
        self.url = f"/api/routes/{self.route.pk}/location/"

    def post(self, lat=NEAR_LAT, lng=-60.0, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url, {"latitude": lat, "longitude": lng, **extra}, format="json")

    def test_grava_ultima_posicao_e_transmite(self):
        r = self.post(heading=90, speed=8.5, accuracy=5)
        self.assertEqual(r.status_code, 202, r.content)
        location = VehicleLocation.objects.get(route=self.route)
        self.assertEqual(location.latitude, Decimal("-3.095000"))
        self.assertEqual(location.heading, 90)

        [payload] = [p for p in self.broadcasts(route_group(self.route.pk)) if p["type"] == "vehicle_location"]
        self.assertEqual(payload["route_id"], self.route.pk)
        self.assertEqual(payload["latitude"], -3.095)
        self.assertEqual(payload["speed"], 8.5)

    def test_atualiza_a_mesma_linha(self):
        self.post()
        self.post(lat=-3.0951234567)
        self.assertEqual(VehicleLocation.objects.filter(route=self.route).count(), 1)
        self.assertEqual(VehicleLocation.objects.get(route=self.route).latitude, Decimal("-3.095123"))

    def test_colaborador_ve_ultima_posicao(self):
        self.post()
        item = self.client_for(self.employee_person).get("/api/me/routes/").json()[0]
        self.assertEqual(item["last_location"]["latitude"], -3.095)

    def test_rota_de_outro_motorista(self):
        r = self.client.post(f"/api/routes/{self.other_route.pk}/location/", {"latitude": 1, "longitude": 1}, format="json")
        self.assertEqual(r.status_code, 404)

    def test_colaborador_nao_envia_posicao(self):
        r = self.client_for(self.employee_person).post(self.url, {"latitude": 1, "longitude": 1}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_coordenada_invalida(self):
        self.assertEqual(self.post(lat=91).status_code, 400)
        self.assertEqual(self.post(heading=400).status_code, 400)

    def test_avisa_chegada_proxima_uma_vez_por_dia(self):
        self.post()
        self.post(lat=NEAR_LAT + 0.0005)
        [notification] = self.notifications_of(self.employee_person, Notification.Kind.BUS_APPROACHING)
        self.assertEqual(notification.title, "Ônibus chegando ao seu ponto")
        self.assertIn("Ponto Central", notification.message)

    def test_nao_avisa_chegada_quando_ja_esta_no_ponto_ou_longe(self):
        self.post(lat=AT_STOP_LAT)
        self.post(lat=-3.05)
        self.assertFalse(self.notifications_of(self.employee_person, Notification.Kind.BUS_APPROACHING).exists())


class RouteEventNotificationTests(RealtimeTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.driver_person)

    def create_event(self, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post("/api/route-events/", self.event_payload(self.route, **extra), format="json")
        self.assertEqual(r.status_code, 201, r.content)

    def test_partida_avisa_passageiros(self):
        self.create_event(event_type="departure")
        [notification] = self.notifications_of(self.employee_person)
        self.assertEqual(notification.kind, Notification.Kind.ROUTE_DEPARTURE)
        self.assertEqual(notification.message, "A Rota A foi iniciada.")
        [event] = [p for p in self.broadcasts(route_group(self.route.pk)) if p["type"] == "route_event"]
        self.assertEqual(event["route_status"], "in_route")
        self.assertEqual(event["route_name"], "Rota A")
        self.assertEqual(event["id"], RouteEvent.objects.get().pk)
        self.assertFalse(self.notifications_of(self.manager_person).exists())

    def test_atraso_leva_descricao(self):
        self.create_event(event_type="delay", description="Trânsito intenso")
        [notification] = self.notifications_of(self.employee_person)
        self.assertEqual(notification.title, "Atraso na Rota A")
        self.assertEqual(notification.message, "Trânsito intenso")

    def test_parada_no_ponto_do_passageiro(self):
        self.create_event(event_type="stop", latitude=str(AT_STOP_LAT), longitude="-60.000000")
        [notification] = self.notifications_of(self.employee_person)
        self.assertEqual(notification.kind, Notification.Kind.BUS_AT_STOP)

    def test_parada_longe_do_ponto_nao_avisa(self):
        self.create_event(event_type="stop", latitude=str(NEAR_LAT), longitude="-60.000000")
        self.assertFalse(self.notifications_of(self.employee_person).exists())

    def test_evento_criado_pelo_admin_tambem_avisa(self):
        with self.captureOnCommitCallbacks(execute=True):
            RouteEvent.objects.create(route=self.route, event_type="arrival", latitude=STOP_LAT, longitude=STOP_LNG)
        [notification] = self.notifications_of(self.employee_person)
        self.assertEqual(notification.kind, Notification.Kind.ROUTE_ARRIVAL)

    def test_motorista_marcando_embarque_transmite_sem_avisar_a_si_mesmo(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(
                f"/api/route-passengers/{self.passenger.pk}/", {"boarding_status": "confirmed"}, format="json"
            )
        [boarding] = [p for p in self.broadcasts(route_group(self.route.pk)) if p["type"] == "boarding_status"]
        self.assertEqual(boarding["status"], "confirmed")
        self.assertFalse(Notification.objects.exists())


class NotificationEndpointTests(RealtimeTestCase):
    def setUp(self):
        super().setUp()
        self.client = self.client_for(self.employee_person)
        self.first = Notification.objects.create(
            recipient=self.employee_person, kind="route_delay", title="A", route=self.route
        )
        self.second = Notification.objects.create(recipient=self.employee_person, kind="route_delay", title="B")
        self.someone_else = Notification.objects.create(recipient=self.driver_person, kind="route_delay", title="C")

    def test_lista_so_as_proprias_mais_recentes_primeiro(self):
        r = self.client.get("/api/notifications/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([n["title"] for n in body["results"]], ["B", "A"])
        self.assertEqual(body["results"][1]["route_id"], self.route.pk)
        self.assertFalse(body["results"][0]["read"])

    def test_marca_como_lida_e_filtra_nao_lidas(self):
        r = self.client.post(f"/api/notifications/{self.first.pk}/read/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["read"])
        unread = self.client.get("/api/notifications/?unread=true").json()["results"]
        self.assertEqual([n["title"] for n in unread], ["B"])

    def test_nao_marca_de_outra_pessoa(self):
        self.assertEqual(self.client.post(f"/api/notifications/{self.someone_else.pk}/read/").status_code, 404)

    def test_marca_todas(self):
        r = self.client.post("/api/notifications/read-all/")
        self.assertEqual(r.json(), {"updated": 2})
        self.someone_else.refresh_from_db()
        self.assertIsNone(self.someone_else.read_at)


@FAST_HASHERS
class StreamConsumerTests(TransactionTestCase):
    """WebSocket ponta a ponta (ASGI + middleware JWT + channel layer em memória)."""

    def setUp(self):
        company = Company.objects.create(name="Acme", email_domain="acme.com")
        self.driver = Person.objects.create(
            company=company, name="Motorista", phone="92999991111", type="DRIVER", license_number="D1"
        )
        self.employee = Person.objects.create(
            company=company, name="Fulano", phone="92999990000", type="EMPLOYEE", employee_code="E1"
        )
        self.outsider = Person.objects.create(
            company=company, name="Ciclano", phone="92999990001", type="EMPLOYEE", employee_code="E2"
        )
        self.route = RoleTestCase.make_route(company, "Rota A", self.driver, "AAA-0001")
        RoutePassenger.objects.create(person=self.employee, route=self.route)

    @staticmethod
    def token_for(person):
        return str(AccessAccountRefreshToken.for_access_account(person.access_account).access_token)

    async def connect(self, token):
        communicator = WebsocketCommunicator(application, f"/ws/stream/?token={token}")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def test_sem_token_fecha_com_4001(self):
        communicator = WebsocketCommunicator(application, "/ws/stream/")
        await communicator.connect()
        self.assertEqual(await communicator.receive_output(), {"type": "websocket.close", "code": 4001})

    async def test_token_invalido_fecha_com_4001(self):
        communicator = await self.connect("nao-e-um-jwt")
        self.assertEqual(await communicator.receive_output(), {"type": "websocket.close", "code": 4001})

    async def test_passageiro_recebe_mensagens_da_rota_e_pessoais(self):
        token = await sync_to_async(self.token_for)(self.employee)
        communicator = await self.connect(token)
        self.assertEqual(await communicator.receive_json_from(), {"type": "connected", "person_id": self.employee.pk})

        layer = get_channel_layer()
        location = {"type": "vehicle_location", "route_id": self.route.pk, "latitude": -3.1}
        await layer.group_send(route_group(self.route.pk), {"type": "stream.message", "payload": location})
        self.assertEqual(await communicator.receive_json_from(), location)

        notification = {"type": "notification", "notification": {"id": 1}}
        await layer.group_send(person_group(self.employee.pk), {"type": "stream.message", "payload": notification})
        self.assertEqual(await communicator.receive_json_from(), notification)
        await communicator.disconnect()

    async def test_quem_nao_e_da_rota_nao_recebe(self):
        token = await sync_to_async(self.token_for)(self.outsider)
        communicator = await self.connect(token)
        await communicator.receive_json_from()  # connected
        await get_channel_layer().group_send(
            route_group(self.route.pk), {"type": "stream.message", "payload": {"type": "vehicle_location"}}
        )
        self.assertTrue(await communicator.receive_nothing(timeout=0.2))
        await communicator.disconnect()

    async def test_motorista_entra_no_grupo_da_propria_rota_e_responde_ping(self):
        token = await sync_to_async(self.token_for)(self.driver)
        communicator = await self.connect(token)
        await communicator.receive_json_from()  # connected
        payload = {"type": "boarding_status", "route_id": self.route.pk, "status": "confirmed"}
        await get_channel_layer().group_send(route_group(self.route.pk), {"type": "stream.message", "payload": payload})
        self.assertEqual(await communicator.receive_json_from(), payload)

        await communicator.send_json_to({"type": "ping"})
        self.assertEqual(await communicator.receive_json_from(), {"type": "pong"})
        await communicator.disconnect()
