from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import (
    AccessAccount,
    Address,
    Company,
    MeetingPoint,
    Person,
    Route,
    RouteEvent,
    RoutePassenger,
    RouteStop,
    Schedule,
    Vehicle,
)


class BaseSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    is_active = serializers.BooleanField(default=True)

    class Meta:
        abstract = True


class CompanySerializer(BaseSerializer):
    class Meta(BaseSerializer.Meta):
        model = Company
        fields = "__all__"


class PersonSerializer(BaseSerializer):
    auto_email = serializers.SerializerMethodField(read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    role = serializers.SerializerMethodField(read_only=True)

    class Meta(BaseSerializer.Meta):
        model = Person
        fields = "__all__"

    def get_auto_email(self, obj):
        return obj.get_auto_email()

    def get_role(self, obj):
        """Perfil de acesso da conta; None quando a pessoa ainda não tem conta."""
        try:
            return obj.access_account.role
        except AccessAccount.DoesNotExist:
            return None

    def create(self, validated_data):
        try:
            instance = super().create(validated_data)
            instance.full_clean()
            instance.save()
            return instance
        except DjangoValidationError as e:
            raise ValidationError(e.message_dict)

    def update(self, instance, validated_data):
        try:
            instance = super().update(instance, validated_data)
            instance.full_clean()
            instance.save()
            return instance
        except DjangoValidationError as e:
            raise ValidationError(e.message_dict)


class AccessRoleSerializer(serializers.Serializer):
    """Corpo de POST people/{id}/access-role/. DRIVER não é atribuível: vem do tipo da pessoa."""

    role = serializers.ChoiceField(
        choices=[AccessAccount.Role.EMPLOYEE, AccessAccount.Role.MANAGER]
    )


class RouteSerializer(BaseSerializer):
    class Meta(BaseSerializer.Meta):
        model = Route
        fields = "__all__"

    def validate(self, attrs):
        attrs = super().validate(attrs)
        company = attrs.get("company") or getattr(self.instance, "company", None)
        schedule = attrs["schedule"] if "schedule" in attrs else getattr(self.instance, "schedule", None)
        if schedule is not None and company is not None and schedule.company_id != company.id:
            raise serializers.ValidationError({"schedule": "O turno deve ser da mesma empresa da rota."})
        return attrs


class AddressSerializer(BaseSerializer):
    class Meta(BaseSerializer.Meta):
        model = Address
        fields = "__all__"
        # Origem e precisão só são preenchidas pelo fluxo do colaborador no app (core.self_address).
        read_only_fields = ("source", "precision", "accuracy_m", "created_by")


class MeetingPointSerializer(BaseSerializer):
    address = AddressSerializer(read_only=True)
    address_id = serializers.IntegerField(write_only=True)

    class Meta(BaseSerializer.Meta):
        model = MeetingPoint
        fields = "__all__"

    def create(self, validated_data):
        validated_data["address_id"] = validated_data.pop("address_id")
        return super().create(validated_data)

    def update(self, instance, validated_data):
        address_id = validated_data.pop("address_id", None)
        if address_id:
            instance.address_id = address_id
        return super().update(instance, validated_data)


class ScheduleSerializer(BaseSerializer):
    class Meta(BaseSerializer.Meta):
        model = Schedule
        fields = "__all__"


class RouteStopSerializer(BaseSerializer):
    meeting_point = MeetingPointSerializer(read_only=True)
    meeting_point_id = serializers.IntegerField(write_only=True)

    class Meta(BaseSerializer.Meta):
        model = RouteStop
        fields = "__all__"


class DriverSerializer(BaseSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)

    class Meta(BaseSerializer.Meta):
        model = Person
        fields = ["id", "company", "company_name", "name", "phone", "license_number", "created_at"]

    def validate_license_number(self, value):
        # PATCH: se não veio no payload, pega do instance
        if value is None and self.instance:
            value = self.instance.license_number

        if not value:
            raise serializers.ValidationError("Driver precisa de license_number.")
        return value

    def validate(self, data):
        data["type"] = "DRIVER"

        # Bloqueia employee_code em DRIVER
        if data.get("employee_code"):
            raise serializers.ValidationError({
                "employee_code": "Driver não pode ter employee_code."
            })

        data["employee_code"] = None
        return data


class EmployeeSerializer(BaseSerializer):
    company_name = serializers.CharField(source="company.name", read_only=True)

    class Meta(BaseSerializer.Meta):
        model = Person
        fields = ["id", "company", "company_name", "name", "phone", "employee_code", "created_at"]

    def validate_employee_code(self, value):
        if value is None and self.instance:
            value = self.instance.employee_code

        if not value:
            raise serializers.ValidationError("Employee precisa de employee_code.")
        return value

    def validate(self, data):
        data["type"] = "EMPLOYEE"

        # Bloqueia license_number em EMPLOYEE
        if data.get("license_number"):
            raise serializers.ValidationError({
                "license_number": "Employee não pode ter license_number."
            })

        data["license_number"] = None
        return data


class RoutePassengerSerializer(BaseSerializer):
    person = PersonSerializer(read_only=True)
    route_stop = RouteStopSerializer(read_only=True)
    person_id = serializers.IntegerField(write_only=True)
    route_stop_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta(BaseSerializer.Meta):
        model = RoutePassenger
        fields = "__all__"

    def validate_person_id(self, person_id):
        person = Person.objects.get(id=person_id)
        if person.type != "EMPLOYEE":
            raise serializers.ValidationError("Somente pessoas do tipo EMPLOYEE podem ser passageiras.")
        return person_id

    def validate_route_stop_id(self, route_stop_id):
        if route_stop_id is None:
            return route_stop_id
        try:
            stop = RouteStop.objects.get(pk=route_stop_id)
        except RouteStop.DoesNotExist:
            raise serializers.ValidationError("Parada não encontrada.")
        route_id = self.initial_data.get("route") or getattr(self.instance, "route_id", None)
        if route_id and stop.route_id != int(route_id):
            raise serializers.ValidationError("A parada não pertence à rota informada.")
        return route_stop_id

    def create(self, validated_data):
        validated_data["person_id"] = validated_data.pop("person_id")
        rs = validated_data.pop("route_stop_id", None)
        if rs is not None:
            validated_data["route_stop_id"] = rs
        return super().create(validated_data)

    def update(self, instance, validated_data):
        person_id = validated_data.pop("person_id", None)
        if person_id is not None:
            instance.person_id = person_id
        if "route_stop_id" in validated_data:
            instance.route_stop_id = validated_data.pop("route_stop_id")
        return super().update(instance, validated_data)



class RouteEventSerializer(BaseSerializer):
    route_name = serializers.CharField(source="route.name", read_only=True)
    latitude = serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        help_text="Latitude em graus decimais. Ex: -23.550520"
    )
    longitude = serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        help_text="Longitude em graus decimais. Ex: -46.633308"
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text=(
            "Obrigatório para eventos do tipo 'delay' e 'incident'. "
            "Opcional para 'departure', 'arrival' e 'stop'."
        )
    )

    class Meta(BaseSerializer.Meta):
        model = RouteEvent
        fields = "__all__"

    def validate_latitude(self, value):
        if value < -90 or value > 90:
            raise serializers.ValidationError("Latitude deve estar entre -90 e 90.")
        return value

    def validate_longitude(self, value):
        if value < -180 or value > 180:
            raise serializers.ValidationError("Longitude deve estar entre -180 e 180.")
        return value

    def validate_description(self, value):
        event_type = self.initial_data.get("event_type") or getattr(self.instance, "event_type", None)

        if event_type in ["delay", "incident"] and not value:
            raise serializers.ValidationError(
                "Descrição é obrigatória para eventos do tipo delay e incident."
            )
        return value



class VehicleSerializer(BaseSerializer):
    driver = PersonSerializer(read_only=True)
    driver_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)

    class Meta(BaseSerializer.Meta):
        model = Vehicle
        fields = "__all__"

    def validate_driver_id(self, driver_id):
        if driver_id is None:
            return driver_id

        try:
            person = Person.objects.get(id=driver_id)
        except Person.DoesNotExist:
            raise serializers.ValidationError("Driver não encontrado.")

        if person.type != "DRIVER":
            raise serializers.ValidationError("Somente pessoas do tipo DRIVER podem ser vinculadas ao veículo.")

        return driver_id

    def create(self, validated_data):
        driver_id = validated_data.pop("driver_id", None)
        if driver_id:
            validated_data["driver_id"] = driver_id
        return super().create(validated_data)

    def update(self, instance, validated_data):
        driver_id = validated_data.pop("driver_id", None)
        if driver_id is not None:
            instance.driver_id = driver_id
        return super().update(instance, validated_data)
