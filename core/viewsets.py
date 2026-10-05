import json
import urllib.error
import urllib.request

from django.conf import settings
from django.db.models import Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.filters import OrderingFilter

from .models import (
    AccessAccount,
    Company,
    Person,
    Route,
    RouteStop,
    RouteEvent,
    Schedule,
    Address,
    MeetingPoint,
    RoutePassenger,
    Vehicle,
)
from .serializers import (
    AccessRoleSerializer,
    CompanySerializer,
    PersonSerializer,
    RouteSerializer,
    RouteStopSerializer,
    ScheduleSerializer,
    AddressSerializer,
    MeetingPointSerializer,
    DriverSerializer,
    EmployeeSerializer,
    RoutePassengerSerializer,
    RouteEventSerializer,
    VehicleSerializer,
)
from .driver_serializers import DriverRouteBoardSerializer
from .permissions import (
    IsDriver,
    IsGerente,
    IsManager,
    IsManagerOrDriver,
    IsManagerOrGerente,
    is_driver,
    is_manager,
    route_belongs_to_driver,
)
from .filters import (
    CompanyFilter,
    PersonFilter,
    DriverFilter,
    EmployeeFilter,
    RouteFilter,
    ScheduleFilter,
    MeetingPointFilter,
    RouteEventFilter,
    RoutePassengerFilter,
    VehicleFilter,
    RouteStopFilter,
)


class BaseViewSet(viewsets.ModelViewSet):
    """
    - Lista apenas registros ativos por padrão
    - DELETE remove do banco (sem soft delete)
    - Apenas gestores (MANAGER); os viewsets que o app do motorista usa afrouxam isso
    """

    permission_classes = [IsAuthenticated, IsManager]
    filter_backends = [DjangoFilterBackend]

    def get_queryset(self):
        qs = super().get_queryset()
        if hasattr(self.queryset.model, "is_active"):
            return qs.filter(is_active=True)
        return qs


class CompanyScopedQuerySetMixin:
    """
    Restringe o gestor (MANAGER) à própria empresa — na leitura (get_queryset) e na
    escrita (perform_create/perform_update, para não aceitar um `company`/`route` de
    outra empresa no payload). O gerente, que não é MANAGER, não é afetado.

    `company_lookup` é "company" (FK direta) ou "<relacao>__company" (ex.: "route__company").
    """

    company_lookup = "company"

    def get_queryset(self):
        qs = super().get_queryset()
        if is_manager(self.request):
            company_id = self.request.user.person.company_id
            qs = qs.filter(**{f"{self.company_lookup}_id": company_id})
        return qs

    def _resolve_target_company_id(self, validated_data, instance):
        if self.company_lookup == "company":
            company = validated_data.get("company", getattr(instance, "company", None))
            return company.pk if company else None
        root_field = self.company_lookup.split("__", 1)[0]
        related = validated_data.get(root_field, getattr(instance, root_field, None))
        return related.company_id if related else None

    def _check_company_scope(self, serializer):
        if not is_manager(self.request):
            return
        target_id = self._resolve_target_company_id(serializer.validated_data, serializer.instance)
        if target_id != self.request.user.person.company_id:
            raise PermissionDenied("Este recurso não pertence à sua empresa.")

    def perform_create(self, serializer):
        self._check_company_scope(serializer)
        serializer.save()

    def perform_update(self, serializer):
        self._check_company_scope(serializer)
        serializer.save()


class CompanyViewSet(BaseViewSet):
    """Cadastro de empresas: exclusivo do gerente (fora do escopo do gestor)."""

    permission_classes = [IsAuthenticated, IsGerente]
    queryset = Company.objects.all()
    serializer_class = CompanySerializer
    filterset_class = CompanyFilter


class PersonViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = Person.objects.select_related("company", "access_account")
    serializer_class = PersonSerializer
    filterset_class = PersonFilter

    # Só o gerente promove alguém a gestor; gestor e gerente cadastram/editam pessoas.
    GERENTE_ACTIONS = ("access_role",)

    def get_permissions(self):
        if self.action in self.GERENTE_ACTIONS:
            return [IsAuthenticated(), IsGerente()]
        return [IsAuthenticated(), IsManagerOrGerente()]

    @action(detail=True, methods=["post"], url_path="access-role")
    def access_role(self, request, pk=None):
        """Define o perfil de acesso (EMPLOYEE ou MANAGER) da conta da pessoa."""
        person = self.get_object()
        serializer = AccessRoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        role = serializer.validated_data["role"]

        if person.type == Person.PersonType.DRIVER:
            raise ValidationError(
                {"role": "O perfil de um motorista é sempre DRIVER."}
            )
        try:
            account = person.access_account
        except AccessAccount.DoesNotExist:
            raise ValidationError(
                {"detail": "A pessoa ainda não tem conta de acesso."}
            )
        if account.pk == request.user.pk and role != AccessAccount.Role.MANAGER:
            raise ValidationError(
                {"role": "Você não pode remover o seu próprio perfil de gestor."}
            )

        account.role = role
        account.save(update_fields=["role", "updated_at"])
        return Response(PersonSerializer(person).data)


class RouteViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = Route.objects.select_related("company", "vehicle")
    serializer_class = RouteSerializer
    filterset_class = RouteFilter

    @action(
        detail=False,
        methods=["get"],
        url_path="my-routes",
        permission_classes=[IsAuthenticated, IsDriver],
    )
    def my_routes(self, request):
        person = request.user.person
        qs = (
            Route.objects.filter(is_active=True, vehicle__driver=person)
            .select_related("company", "vehicle")
            .prefetch_related(
                Prefetch(
                    "stops",
                    queryset=RouteStop.objects.filter(is_active=True)
                    .select_related("meeting_point", "meeting_point__address")
                    .order_by("sequence"),
                ),
                Prefetch(
                    "route_passengers",
                    queryset=RoutePassenger.objects.filter(is_active=True).select_related(
                        "person", "route_stop", "route_stop__meeting_point"
                    ),
                ),
                Prefetch(
                    "events",
                    queryset=RouteEvent.objects.order_by("-timestamp"),
                ),
            )
            .order_by("id")
        )
        ser = DriverRouteBoardSerializer()
        return Response([ser.to_representation(r) for r in qs])

    @action(
        detail=True,
        methods=["get"],
        url_path="driver-detail",
        permission_classes=[IsAuthenticated, IsDriver],
    )
    def driver_detail(self, request, pk=None):
        person = request.user.person
        route = get_object_or_404(
            Route.objects.filter(is_active=True, vehicle__driver=person)
            .select_related("company", "vehicle")
            .prefetch_related(
                Prefetch(
                    "stops",
                    queryset=RouteStop.objects.filter(is_active=True)
                    .select_related("meeting_point", "meeting_point__address")
                    .order_by("sequence"),
                ),
                Prefetch(
                    "route_passengers",
                    queryset=RoutePassenger.objects.filter(is_active=True).select_related(
                        "person", "route_stop", "route_stop__meeting_point"
                    ),
                ),
                Prefetch(
                    "events",
                    queryset=RouteEvent.objects.order_by("-timestamp"),
                ),
            ),
            pk=pk,
        )
        return Response(DriverRouteBoardSerializer().to_representation(route))

    @action(
        detail=True,
        methods=["get"],
        permission_classes=[IsAuthenticated, IsManagerOrDriver],
    )
    def events(self, request, pk=None):
        route = get_object_or_404(
            Route.objects.filter(is_active=True).select_related("vehicle"), pk=pk
        )
        if is_driver(request) and not route_belongs_to_driver(route, request.user.person):
            # 404 (e não 403) para não revelar a existência de rotas de outros motoristas
            raise Http404
        if is_manager(request) and route.company_id != request.user.person.company_id:
            raise Http404
        qs = route.events.order_by("-timestamp")
        serializer = RouteEventSerializer(qs, many=True)
        return Response(serializer.data)

    @action(
        detail=True,
        methods=["get"],
        url_path="navigation",
        permission_classes=[IsAuthenticated, IsDriver],
    )
    def navigation(self, request, pk=None):
        person = request.user.person

        route = get_object_or_404(
            Route.objects.filter(is_active=True, vehicle__driver=person)
            .select_related("company", "vehicle")
            .prefetch_related(
                Prefetch(
                    "stops",
                    queryset=RouteStop.objects.filter(is_active=True)
                    .select_related("meeting_point", "meeting_point__address")
                    .order_by("sequence"),
                )
            ),
            pk=pk,
        )

        if not settings.GOOGLE_MAPS_API_KEY:
            return Response(
                {"detail": "GOOGLE_MAPS_API_KEY não configurada."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        origin = _address_to_location(
            Address.objects.filter(is_active=True, person=person).first()
        )
        destination = _address_to_location(
            Address.objects.filter(
                is_active=True,
                person__isnull=True,
                company=route.company,
            )
            .exclude(
                id__in=route.stops.values_list(
                    "meeting_point__address_id",
                    flat=True,
                )
            )
            .first()
        )
        stops = [
            _address_to_location(stop.meeting_point.address)
            for stop in route.stops.all()
            if stop.meeting_point and stop.meeting_point.address
        ]
        stops = [stop for stop in stops if stop is not None]

        if origin is None or destination is None:
            return Response(
                {"detail": "Origem ou destino da rota sem coordenadas."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            google_payload = _compute_google_route(
                origin=origin,
                destination=destination,
                intermediates=stops,
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            return Response(
                {"detail": "Falha ao calcular rota no Google.", "error": body},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except urllib.error.URLError as exc:
            return Response(
                {"detail": "Google Routes API indisponível.", "error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        route_data = (google_payload.get("routes") or [{}])[0]
        polyline = route_data.get("polyline") or {}
        legs = route_data.get("legs") or []
        first_step = _first_navigation_step(legs)

        return Response(
            {
                "route": route.id,
                "encoded_polyline": polyline.get("encodedPolyline", ""),
                "duration": route_data.get("duration"),
                "duration_text": _format_duration(route_data.get("duration")),
                "distance_meters": route_data.get("distanceMeters"),
                "distance_text": _format_distance(route_data.get("distanceMeters")),
                "instruction": first_step,
                "waypoints": {
                    "origin": origin,
                    "intermediates": stops,
                    "destination": destination,
                },
            }
        )


class ScheduleViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = Schedule.objects.select_related("company")
    serializer_class = ScheduleSerializer
    filterset_class = ScheduleFilter


class AddressViewSet(BaseViewSet):
    """
    Sem `company_lookup` fixo: um endereço é da empresa (ponto de encontro) OU de uma
    pessoa (endereço residencial), nunca os dois — o escopo do gestor precisa cobrir os dois casos.
    """

    queryset = Address.objects.all()
    serializer_class = AddressSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if is_manager(self.request):
            company_id = self.request.user.person.company_id
            qs = qs.filter(Q(company_id=company_id) | Q(person__company_id=company_id))
        return qs

    def _target_company_id(self, serializer):
        data = serializer.validated_data
        instance = serializer.instance
        company = data.get("company", getattr(instance, "company", None))
        if company:
            return company.pk
        person = data.get("person", getattr(instance, "person", None))
        return person.company_id if person else None

    def _check_company_scope(self, serializer):
        if is_manager(self.request):
            company_id = self.request.user.person.company_id
            if self._target_company_id(serializer) != company_id:
                raise PermissionDenied("Este endereço não pertence à sua empresa.")

    def perform_create(self, serializer):
        self._check_company_scope(serializer)
        serializer.save()

    def perform_update(self, serializer):
        self._check_company_scope(serializer)
        serializer.save()


class MeetingPointViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = MeetingPoint.objects.select_related("company", "address")
    serializer_class = MeetingPointSerializer
    filterset_class = MeetingPointFilter


class DriverViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = Person.objects.filter(type="DRIVER").select_related("company")
    serializer_class = DriverSerializer
    filterset_class = DriverFilter


class EmployeeViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = Person.objects.filter(type="EMPLOYEE").select_related("company")
    serializer_class = EmployeeSerializer
    filterset_class = EmployeeFilter


class RoutePassengerViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = RoutePassenger.objects.select_related(
        "person", "person__company", "person__access_account", "route", "route_stop"
    )
    serializer_class = RoutePassengerSerializer
    filterset_class = RoutePassengerFilter
    company_lookup = "route__company"

    # O motorista consulta os passageiros das próprias rotas e só marca o embarque (PATCH).
    DRIVER_ACTIONS = ("list", "retrieve", "partial_update")
    DRIVER_EDITABLE_FIELDS = {"boarding_status"}

    def get_permissions(self):
        if self.action in self.DRIVER_ACTIONS:
            return [IsAuthenticated(), IsManagerOrDriver()]
        return super().get_permissions()

    def get_queryset(self):
        qs = super().get_queryset()
        if is_driver(self.request):
            qs = qs.filter(route__vehicle__driver=self.request.user.person)
        return qs

    def partial_update(self, request, *args, **kwargs):
        if is_driver(request):
            extra = set(request.data.keys()) - self.DRIVER_EDITABLE_FIELDS
            if extra:
                raise PermissionDenied(
                    "Motoristas só podem alterar o boarding_status do passageiro."
                )
        return super().partial_update(request, *args, **kwargs)


class RouteStopViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = RouteStop.objects.select_related("route", "meeting_point", "meeting_point__address")
    serializer_class = RouteStopSerializer
    filterset_class = RouteStopFilter
    company_lookup = "route__company"


class RouteEventViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = RouteEvent.objects.select_related("route")
    serializer_class = RouteEventSerializer
    company_lookup = "route__company"

    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = RouteEventFilter
    ordering_fields = ["timestamp", "event_type"]
    ordering = ["-timestamp"]

    # O motorista registra e consulta eventos apenas das próprias rotas.
    DRIVER_ACTIONS = ("list", "retrieve", "create")

    def get_permissions(self):
        if self.action in self.DRIVER_ACTIONS:
            return [IsAuthenticated(), IsManagerOrDriver()]
        return super().get_permissions()

    def get_queryset(self):
        qs = super().get_queryset()
        if is_driver(self.request):
            qs = qs.filter(route__vehicle__driver=self.request.user.person)
        return qs

    def perform_create(self, serializer):
        route = serializer.validated_data["route"]
        if is_driver(self.request) and not route_belongs_to_driver(
            route, self.request.user.person
        ):
            raise PermissionDenied("Esta rota não está atribuída a você.")
        if is_manager(self.request) and route.company_id != self.request.user.person.company_id:
            raise PermissionDenied("Esta rota não pertence à sua empresa.")
        serializer.save()


class VehicleViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = Vehicle.objects.select_related("company", "driver", "driver__access_account")
    serializer_class = VehicleSerializer
    filterset_class = VehicleFilter


def _address_to_location(address):
    if not address or address.latitude is None or address.longitude is None:
        return None

    return {
        "latitude": float(address.latitude),
        "longitude": float(address.longitude),
    }


def _compute_google_route(origin, destination, intermediates):
    payload = {
        "origin": {"location": {"latLng": origin}},
        "destination": {"location": {"latLng": destination}},
        "intermediates": [
            {"location": {"latLng": waypoint}}
            for waypoint in intermediates
        ],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "languageCode": "pt-BR",
        "units": "METRIC",
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        settings.GOOGLE_ROUTES_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
            "X-Goog-FieldMask": (
                "routes.duration,"
                "routes.distanceMeters,"
                "routes.polyline.encodedPolyline,"
                "routes.legs.steps.navigationInstruction"
            ),
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _first_navigation_step(legs):
    for leg in legs:
        for step in leg.get("steps") or []:
            instruction = step.get("navigationInstruction") or {}
            text = instruction.get("instructions")
            if text:
                return text
    return "Siga a rota"


def _format_duration(duration):
    if not duration or not isinstance(duration, str) or not duration.endswith("s"):
        return None

    try:
        seconds = int(float(duration[:-1]))
    except ValueError:
        return None

    minutes = max(1, round(seconds / 60))
    return f"{minutes} min"


def _format_distance(distance_meters):
    if distance_meters is None:
        return None

    try:
        meters = float(distance_meters)
    except (TypeError, ValueError):
        return None

    if meters >= 1000:
        return f"{meters / 1000:.1f} km"
    return f"{round(meters)} m"