import json
import urllib.error
import urllib.request

from django.conf import settings
from django.db.models import F, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.filters import OrderingFilter
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from .boarding import record_boarding
from .dates import local_today
from .driver_serializers import DriverRouteBoardSerializer
from .eta import estimate as eta_estimate
from .filters import (
    CompanyFilter,
    DriverFilter,
    EmployeeFilter,
    MeetingPointFilter,
    PersonFilter,
    RouteEventFilter,
    RouteFilter,
    RoutePassengerFilter,
    RouteStopFilter,
    ScheduleFilter,
    VehicleFilter,
)
from .importacao_csv import ImportErrors, import_people
from .models import (
    AccessAccount,
    Address,
    Company,
    MeetingPoint,
    Notification,
    Person,
    Route,
    RouteEvent,
    RoutePassenger,
    RouteStop,
    Schedule,
    Vehicle,
)
from .notifications import on_boarding_status_changed, record_vehicle_location
from .permissions import (
    IsDriver,
    IsGerente,
    IsManager,
    IsManagerOrDriver,
    IsManagerOrGerente,
    IsPassengerPerson,
    is_driver,
    is_manager,
    route_belongs_to_driver,
)
from .person_overview import person_overview
from .realtime_serializers import (
    BoardingStatusInputSerializer,
    CollaboratorRouteSerializer,
    LocationInputSerializer,
    NotificationSerializer,
    location_payload,
)
from .serializers import (
    AccessRoleSerializer,
    AddressSerializer,
    CompanySerializer,
    DriverSerializer,
    EmployeeSerializer,
    MeetingPointSerializer,
    PersonSerializer,
    RouteEventSerializer,
    RoutePassengerSerializer,
    RouteSerializer,
    RouteStopSerializer,
    ScheduleSerializer,
    VehicleSerializer,
)


class VehicleLocationThrottle(UserRateThrottle):
    """Limite próprio para o GPS do motorista (DEFAULT_THROTTLE_RATES["vehicle_location"])."""

    scope = "vehicle_location"


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
        if hasattr(self.queryset.model, "is_active") and self.active_only():
            return qs.filter(is_active=True)
        return qs

    def active_only(self) -> bool:
        """Esconde os inativos; um viewset pode liberar (ex.: filtro de status de Funcionários)."""
        return True


IMPORT_MAX_BYTES = 2_000_000  # ~10 mil linhas; o limite real de linhas está em importacao_csv.MAX_ROWS


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

    def active_only(self):
        # Lista: só ativos, a menos que o filtro peça ?status=inactive ou ?status=all (PersonFilter).
        # Detalhe/edição: inativos também, para abrir e reativar a pessoa pela lista.
        if self.action == "list":
            return self.request.query_params.get("status") not in ("inactive", "all")
        return False

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser])
    def import_csv(self, request):
        """
        POST people/import/ (multipart: file, confirm=true|false, company=<id> só para o gerente).

        Sem confirm, só valida e devolve a prévia; com confirm=true, grava tudo ou nada.
        O gestor importa na própria empresa e não cria gestor nem gerente (só o gerente promove).
        """
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Envie o arquivo CSV no campo file."})
        if upload.size > IMPORT_MAX_BYTES:
            raise ValidationError({"file": f"Arquivo maior que {IMPORT_MAX_BYTES // 1_000_000} MB."})
        if is_manager(request):
            company = request.user.person.company
            allowed = ("EMPRESA", "COLABORADOR", "MOTORISTA")
        else:
            company = Company.objects.filter(pk=request.data.get("company") or 0).first()
            if company is None:
                raise ValidationError({"company": "Escolha a empresa que vai receber a importação."})
            allowed = ("EMPRESA", "COLABORADOR", "MOTORISTA", "GESTOR")
        confirm = str(request.data.get("confirm", "")).lower() in ("1", "true", "sim")
        try:
            result = import_people(upload.read(), company=company, dry_run=not confirm, allowed_profiles=allowed)
        except ImportErrors as exc:
            return Response({"errors": exc.errors}, status=status.HTTP_400_BAD_REQUEST)
        return Response({
            "confirmed": confirm,
            "company": {"id": company.pk, "name": company.name, "email_domain": company.get_email_domain()},
            "created": {
                "employees": result.created["COLABORADOR"], "drivers": result.created["MOTORISTA"],
                "managers": result.created["GESTOR"],
            },
            "vehicles": result.vehicles,
            "skipped": result.skipped,
            "company_address": result.company_address,
        }, status=status.HTTP_201_CREATED if confirm else status.HTTP_200_OK)

    @action(detail=True, methods=["get"])
    def overview(self, request, pk=None):
        """GET people/{id}/overview/ — conta, endereço, rotas atuais e outros cadastros da mesma pessoa."""
        return Response(person_overview(self.get_object()))

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
            .filter(Q(service_date__isnull=True) | Q(service_date__gte=local_today()))
            .select_related("company", "vehicle", "schedule")
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
            # Fixas primeiro, depois avulsas por data; dentro disso, turno mais cedo primeiro.
            .order_by(
                F("service_date").asc(nulls_first=True), F("schedule__time").asc(nulls_last=True), "id"
            )
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
            .select_related("company", "vehicle", "schedule")
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
        methods=["post"],
        url_path="location",
        permission_classes=[IsAuthenticated, IsDriver],
        throttle_classes=[VehicleLocationThrottle],
    )
    def location(self, request, pk=None):
        """GPS do motorista em rota: guarda a última posição e repassa pelo WebSocket."""
        route = get_object_or_404(
            Route.objects.filter(is_active=True, vehicle__driver=request.user.person), pk=pk
        )
        serializer = LocationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        location = record_vehicle_location(route, data)
        return Response(location_payload(location), status=status.HTTP_202_ACCEPTED)

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

        # ?origin=lat,lng: posição atual do motorista (GPS). Sem ela, parte do endereço dele.
        origin_param = request.query_params.get("origin")
        if origin_param:
            origin = _parse_lat_lng(origin_param)
            if origin is None:
                return Response(
                    {"detail": "Parâmetro origin inválido. Use origin=latitude,longitude."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            origin_source = "gps"
        else:
            origin = _address_to_location(
                Address.objects.filter(is_active=True, person=person).first()
            )
            origin_source = "home"
        # ?done_stops=1,2: paradas já atendidas nesta viagem; ficam fora do trajeto.
        done_stop_ids = _parse_id_list(request.query_params.get("done_stops"))

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
        route_stops = []
        for stop in route.stops.all():
            location = _address_to_location(
                stop.meeting_point.address if stop.meeting_point else None
            )
            if location is None:
                continue
            route_stops.append(
                {
                    "id": stop.id,
                    "sequence": stop.sequence,
                    "title": stop.meeting_point.name,
                    **location,
                    "done": stop.id in done_stop_ids,
                }
            )
        pending_stops = [stop for stop in route_stops if not stop["done"]]
        stops = [
            {"latitude": stop["latitude"], "longitude": stop["longitude"]}
            for stop in pending_stops
        ]

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
                "origin_source": origin_source,
                "waypoints": {
                    "origin": origin,
                    "intermediates": stops,
                    "destination": destination,
                },
                "destination_name": route.company.name,
                "stops": route_stops,
                # Trecho i termina na i-ésima parada pendente; o último, no destino.
                "legs": [
                    {
                        "stop_id": pending_stops[index]["id"] if index < len(pending_stops) else None,
                        "distance_meters": leg.get("distanceMeters", 0),
                        "duration_seconds": _duration_seconds(leg.get("duration")) or 0,
                    }
                    for index, leg in enumerate(legs)
                ],
                "steps": _navigation_steps(legs),
            }
        )


class ScheduleViewSet(CompanyScopedQuerySetMixin, BaseViewSet):
    queryset = Schedule.objects.select_related("company").order_by("time", "id")
    serializer_class = ScheduleSerializer
    filterset_class = ScheduleFilter

    def active_only(self):
        # Lista: só ativos, a menos que o filtro peça ?status=inactive ou ?status=all (ScheduleFilter).
        # Detalhe/edição: inativos também, para abrir e reativar o turno pela lista.
        if self.action == "list":
            return self.request.query_params.get("status") not in ("inactive", "all")
        return False


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

    def perform_update(self, serializer):
        previous_status = serializer.instance.boarding_status
        super().perform_update(serializer)
        if serializer.instance.boarding_status != previous_status:
            record_boarding(serializer.instance, marked_by=self.request.user.person)
            on_boarding_status_changed(serializer.instance, by_passenger=False)


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


class CollaboratorRouteViewSet(viewsets.GenericViewSet):
    """
    App do colaborador: as rotas em que ele é passageiro (`me/routes/`) e a
    confirmação de presença (`PATCH me/routes/{route_passenger_id}/`).
    """

    permission_classes = [IsAuthenticated, IsPassengerPerson]
    pagination_class = None
    filter_backends = []

    def get_queryset(self):
        return (
            RoutePassenger.objects.filter(
                person=self.request.user.person, is_active=True, route__is_active=True
            )
            .filter(Q(route__service_date__isnull=True) | Q(route__service_date__gte=local_today()))
            .select_related(
                "route__company",
                "route__vehicle__driver",
                "route__current_location",
                "route__schedule",
                "route_stop__meeting_point__address",
            )
            .order_by(
                F("route__service_date").asc(nulls_first=True),
                F("route__schedule__time").asc(nulls_last=True),
                "route__name",
                "id",
            )
        )

    def list(self, request):
        serializer = CollaboratorRouteSerializer()
        return Response([serializer.to_representation(rp) for rp in self.get_queryset()])

    @action(detail=True, methods=["get"], url_path="eta")
    def eta(self, request, pk=None):
        """
        GET me/routes/{route_passenger_id}/eta/ — previsão de chegada do ônibus ao ponto,
        pelo trajeto das ruas. O app consulta a cada posição nova recebida pelo WebSocket.
        """
        passenger = get_object_or_404(self.get_queryset(), pk=pk)
        location = getattr(passenger.route, "current_location", None)
        return Response({
            "route_passenger_id": passenger.id,
            "eta": eta_estimate(passenger),
            "last_location": location_payload(location),
        })

    def partial_update(self, request, pk=None):
        passenger = get_object_or_404(self.get_queryset(), pk=pk)
        extra = set(request.data.keys()) - {"boarding_status"}
        if extra:
            raise ValidationError({"detail": "Só é possível alterar o boarding_status."})
        serializer = BoardingStatusInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_status = serializer.validated_data["boarding_status"]
        if new_status != passenger.boarding_status:
            passenger.boarding_status = new_status
            passenger.save(update_fields=["boarding_status", "updated_at"])
            record_boarding(passenger, marked_by=request.user.person)
            on_boarding_status_changed(passenger, by_passenger=True)
        return Response(CollaboratorRouteSerializer().to_representation(passenger))


class NotificationViewSet(viewsets.GenericViewSet):
    """Avisos da pessoa logada (qualquer perfil). `?unread=true` filtra as não lidas."""

    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = []

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user.person)

    def list(self, request):
        qs = self.get_queryset()
        if request.query_params.get("unread", "").lower() in ("1", "true"):
            qs = qs.filter(read_at__isnull=True)
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        notification = get_object_or_404(self.get_queryset(), pk=pk)
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at"])
        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        updated = self.get_queryset().filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({"updated": updated})


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
                "routes.legs.duration,"
                "routes.legs.distanceMeters,"
                "routes.legs.steps.distanceMeters,"
                "routes.legs.steps.startLocation,"
                "routes.legs.steps.endLocation,"
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


def _navigation_steps(legs):
    """Passos de todos os trechos, na ordem, com o índice do trecho e onde começam/terminam."""
    steps = []
    for leg_index, leg in enumerate(legs):
        for step in leg.get("steps") or []:
            instruction = step.get("navigationInstruction") or {}
            start = (step.get("startLocation") or {}).get("latLng")
            end = (step.get("endLocation") or {}).get("latLng")
            if not start or not end:
                continue
            steps.append(
                {
                    "leg_index": leg_index,
                    "instruction": instruction.get("instructions") or "Siga em frente",
                    "maneuver": instruction.get("maneuver") or "",
                    "distance_meters": step.get("distanceMeters", 0),
                    "start": start,
                    "end": end,
                }
            )
    return steps


def _parse_lat_lng(value):
    try:
        latitude, longitude = (float(part) for part in value.split(","))
    except ValueError:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {"latitude": latitude, "longitude": longitude}


def _parse_id_list(value):
    if not value:
        return set()
    return {int(part) for part in value.split(",") if part.strip().isdigit()}


def _duration_seconds(duration):
    """A Routes API devolve durações como "123s"."""
    if not duration or not isinstance(duration, str) or not duration.endswith("s"):
        return None

    try:
        return int(float(duration[:-1]))
    except ValueError:
        return None


def _format_duration(duration):
    seconds = _duration_seconds(duration)
    if seconds is None:
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
