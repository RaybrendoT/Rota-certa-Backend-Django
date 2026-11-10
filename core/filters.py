import django_filters
from django.db.models import Q

from .models import Company, MeetingPoint, Person, Route, RouteEvent, RoutePassenger, RouteStop, Schedule, Vehicle


class CompanyFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")

    class Meta:
        model = Company
        fields = ["is_active"]


class PersonFilter(django_filters.FilterSet):
    company_id = django_filters.NumberFilter(field_name="company_id")
    type = django_filters.CharFilter(field_name="type")
    # Perfil de acesso vive em AccessAccount, não em Person; usado para listar gestores
    # (?role=MANAGER) e para checar se uma empresa já tem gestor (?company_id=X&role=MANAGER).
    role = django_filters.CharFilter(field_name="access_account__role")
    # Filtros da tela de Funcionários. Tipo de acesso: DRIVER (motorista), EMPLOYEE (colaborador
    # sem perfil de gestão), MANAGER (gestor) ou GERENTE.
    access = django_filters.ChoiceFilter(
        choices=[("DRIVER", "Motorista"), ("EMPLOYEE", "Funcionário"), ("MANAGER", "Gestor"), ("GERENTE", "Gerente")],
        method="filter_access",
    )
    # active (padrão, o mesmo de antes), inactive ou all. Ver PersonViewSet.active_only.
    status = django_filters.ChoiceFilter(
        choices=[("active", "Ativo"), ("inactive", "Inativo"), ("all", "Todos")], method="filter_status"
    )
    # Nome, matrícula ou CNH, sem diferenciar maiúsculas.
    search = django_filters.CharFilter(method="filter_search")

    class Meta:
        model = Person
        fields = ["company_id", "type", "role", "is_active"]

    def filter_access(self, queryset, name, value):
        if value == "DRIVER":
            return queryset.filter(type=Person.PersonType.DRIVER)
        if value == "EMPLOYEE":
            return queryset.filter(type=Person.PersonType.EMPLOYEE).filter(
                Q(access_account__isnull=True) | Q(access_account__role=value)
            )
        return queryset.filter(access_account__role=value)

    def filter_status(self, queryset, name, value):
        if value == "inactive":
            return queryset.filter(is_active=False)
        if value == "active":
            return queryset.filter(is_active=True)
        return queryset

    def filter_search(self, queryset, name, value):
        value = value.strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(name__icontains=value) | Q(employee_code__icontains=value) | Q(license_number__icontains=value)
        )


class DriverFilter(django_filters.FilterSet):
    company_id = django_filters.NumberFilter(field_name="company_id")

    class Meta:
        model = Person
        fields = ["company_id", "is_active"]

    @property
    def qs(self):
        parent_qs = super().qs
        return parent_qs.filter(type="DRIVER")


class EmployeeFilter(django_filters.FilterSet):
    company_id = django_filters.NumberFilter(field_name="company_id")

    class Meta:
        model = Person
        fields = ["company_id", "is_active"]

    @property
    def qs(self):
        parent_qs = super().qs
        return parent_qs.filter(type="EMPLOYEE")


class RouteFilter(django_filters.FilterSet):
    company_id = django_filters.NumberFilter(field_name="company_id")
    name = django_filters.CharFilter(lookup_expr="icontains")
    vehicle_id = django_filters.NumberFilter(field_name="vehicle_id")
    # fixed = rota diária (sem data, repete nos dias do turno); extra = avulsa, montada para um dia.
    kind = django_filters.ChoiceFilter(choices=(("fixed", "Fixa"), ("extra", "Avulsa")), method="filter_kind")

    def filter_kind(self, queryset, name, value):
        return queryset.filter(service_date__isnull=value == "fixed")

    class Meta:
        model = Route
        fields = ["company_id", "vehicle_id", "is_active"]


class RouteStopFilter(django_filters.FilterSet):
    route_id = django_filters.NumberFilter(field_name="route_id")

    class Meta:
        model = RouteStop
        fields = ["route_id", "is_active"]


class ScheduleFilter(django_filters.FilterSet):
    company_id = django_filters.NumberFilter(field_name="company_id")

    class Meta:
        model = Schedule
        fields = ["company_id", "is_active"]


class MeetingPointFilter(django_filters.FilterSet):
    company_id = django_filters.NumberFilter(field_name="company_id")
    city = django_filters.CharFilter(field_name="address__city", lookup_expr="icontains")

    class Meta:
        model = MeetingPoint
        fields = ["company_id", "is_active"]


class RouteEventFilter(django_filters.FilterSet):
    route_id = django_filters.NumberFilter(field_name="route__id")
    event_type = django_filters.BaseInFilter(field_name="event_type", lookup_expr="in")

    class Meta:
        model = RouteEvent
        fields = ["route_id", "event_type"]


class RoutePassengerFilter(django_filters.FilterSet):
    route_id = django_filters.NumberFilter(field_name="route_id")
    person_id = django_filters.NumberFilter(field_name="person_id")
    boarding_status = django_filters.CharFilter(field_name="boarding_status")

    class Meta:
        model = RoutePassenger
        fields = ["route_id", "person_id", "boarding_status", "is_active"]

class VehicleFilter(django_filters.FilterSet):
    company_id = django_filters.NumberFilter(field_name="company_id")
    driver_id = django_filters.NumberFilter(field_name="driver_id")
    plate_number = django_filters.CharFilter(lookup_expr="icontains")
    model = django_filters.CharFilter(lookup_expr="icontains")
    min_capacity = django_filters.NumberFilter(field_name="capacity", lookup_expr="gte")
    max_capacity = django_filters.NumberFilter(field_name="capacity", lookup_expr="lte")

    class Meta:
        model = Vehicle
        fields = [
            "company_id",
            "driver_id",
            "plate_number",
            "model",
            "min_capacity",
            "max_capacity",
            "is_active",
        ]
