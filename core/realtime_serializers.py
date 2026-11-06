"""Serializers do app mobile em tempo real: notificações, localização e visão do colaborador."""
from rest_framework import serializers

from . import eta
from .driver_serializers import _route_status_from_events, schedule_payload
from .models import Notification, RoutePassenger, Schedule


class NotificationSerializer(serializers.ModelSerializer):
    route_id = serializers.IntegerField(read_only=True, allow_null=True)
    read = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = ["id", "kind", "title", "message", "route_id", "data", "created_at", "read"]
        read_only_fields = fields

    def get_read(self, obj) -> bool:
        return obj.read_at is not None


class LocationInputSerializer(serializers.Serializer):
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    heading = serializers.FloatField(min_value=0, max_value=360, required=False, allow_null=True)
    speed = serializers.FloatField(min_value=0, required=False, allow_null=True)
    accuracy = serializers.FloatField(min_value=0, required=False, allow_null=True)


def location_payload(location) -> dict | None:
    if location is None:
        return None
    return {
        "route_id": location.route_id,
        "latitude": float(location.latitude),
        "longitude": float(location.longitude),
        "heading": location.heading,
        "speed": location.speed,
        "recorded_at": location.recorded_at.isoformat(),
    }


class BoardingStatusInputSerializer(serializers.Serializer):
    boarding_status = serializers.ChoiceField(choices=RoutePassenger.BoardingStatus.choices)


class CollaboratorRouteSerializer(serializers.BaseSerializer):
    """Vínculo do colaborador com uma rota: parada de embarque, ônibus, motorista e horários."""

    def to_representation(self, rp):
        route = rp.route
        vehicle = route.vehicle
        stop = rp.route_stop

        stop_out = None
        if stop is not None:
            mp = stop.meeting_point
            addr = mp.address
            stop_out = {
                "id": stop.id,
                "sequence": stop.sequence,
                # Embarque na porta de casa (core.home_pickup): para o próprio colaborador é "Sua casa".
                "title": "Sua casa" if mp.home_of_id is not None and mp.home_of_id == rp.person_id else mp.name,
                "subtitle": f"{addr.street}, {addr.number} — {addr.city}/{addr.state}",
                "latitude": float(addr.latitude),
                "longitude": float(addr.longitude),
            }

        # Rota com turno: a agenda do colaborador é o horário dela; sem turno, os horários da empresa.
        if route.schedule is not None:
            schedules = [route.schedule]
        else:
            schedules = Schedule.objects.filter(company_id=route.company_id, is_active=True).order_by("time")

        return {
            "id": rp.id,
            "boarding_status": rp.boarding_status,
            "route": {
                "id": route.id,
                "name": route.name,
                "company_name": route.company.name,
                "route_status": _route_status_from_events(route),
                "vehicle": (
                    {"plate_number": vehicle.plate_number, "model": vehicle.model}
                    if vehicle
                    else None
                ),
                "driver_name": vehicle.driver.name if vehicle and vehicle.driver else None,
                "schedule": schedule_payload(route.schedule),
                "service_date": route.service_date.isoformat() if route.service_date else None,
            },
            "stop": stop_out,
            "last_location": location_payload(getattr(route, "current_location", None)),
            # Previsão de chegada ao ponto e trajeto pelas ruas (core/eta.py).
            **eta.passenger_payload(rp),
            "schedules": [
                {"weekdays": s.weekdays, "time": s.time.strftime("%H:%M")} for s in schedules
            ],
        }
