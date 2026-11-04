from rest_framework import serializers


def route_status_for_event_type(event_type):
    """Heurística simples para UI (Em rota / Aguardando / Atrasado) a partir do último evento."""
    if event_type in ("delay", "incident"):
        return "delayed"
    if event_type == "departure":
        return "in_route"
    return "waiting"


def _route_status_from_events(route):
    ev = route.events.order_by("-timestamp").first()
    return route_status_for_event_type(ev.event_type) if ev else "waiting"


class DriverRouteBoardSerializer(serializers.BaseSerializer):
    """
    Payload alinhado ao app motorista: paradas ordenadas (timeline / RouteStopCard)
    e passageiros (PassengerCard: name, point, status).
    """

    def to_representation(self, route):
        stops_out = []
        for stop in (
            route.stops.select_related("meeting_point", "meeting_point__address")
            .filter(is_active=True)
            .order_by("sequence")
        ):
            mp = stop.meeting_point
            addr = mp.address
            subtitle = f"{addr.street}, {addr.number} — {addr.city}/{addr.state}"
            stops_out.append(
                {
                    "id": stop.id,
                    "sequence": stop.sequence,
                    "title": mp.name,
                    "subtitle": subtitle,
                    "latitude": addr.latitude,
                    "longitude": addr.longitude,
                    "passenger_count": stop.passengers.filter(is_active=True).count(),
                }
            )

        passengers_out = []
        for rp in route.route_passengers.select_related(
            "person", "route_stop", "route_stop__meeting_point"
        ).filter(is_active=True):
            if rp.route_stop_id:
                point = rp.route_stop.meeting_point.name
            else:
                point = route.name
            passengers_out.append(
                {
                    "id": rp.id,
                    "person_id": rp.person_id,
                    "name": rp.person.name,
                    "point": point,
                    "status": rp.boarding_status,
                    "route_stop_id": rp.route_stop_id,
                }
            )

        vehicle = route.vehicle
        return {
            "id": route.id,
            "name": route.name,
            "company_id": route.company_id,
            "company_name": route.company.name,
            "vehicle": (
                {
                    "id": vehicle.id,
                    "plate_number": vehicle.plate_number,
                    "model": vehicle.model,
                }
                if vehicle
                else None
            ),
            "route_status": _route_status_from_events(route),
            "eta_next_stop": None,
            "schedule": schedule_payload(route.schedule),
            "service_date": route.service_date.isoformat() if route.service_date else None,
            "stops": stops_out,
            "passengers": passengers_out,
        }


def schedule_payload(schedule):
    """Turno da rota: horário de chegada na empresa e dias da semana."""
    if schedule is None:
        return None
    return {"id": schedule.id, "time": schedule.time.strftime("%H:%M"), "weekdays": schedule.weekdays}
