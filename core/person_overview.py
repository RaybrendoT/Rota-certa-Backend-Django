"""Detalhes de uma pessoa para o painel lateral do cadastro: conta, endereço, rotas e outros vínculos."""
from django.db.models import Q

from .dates import local_today
from .models import AccessAccount, Person, Route, RoutePassenger
from .self_address import address_payload


def person_overview(person: Person) -> dict:
    today = local_today()
    current = Q(route__service_date__isnull=True) | Q(route__service_date__gte=today)

    routes = []
    rides = (
        RoutePassenger.objects.filter(current, person=person, is_active=True, route__is_active=True)
        .select_related("route__schedule", "route_stop__meeting_point")
    )
    for ride in rides:
        stop = ride.route_stop.meeting_point.name if ride.route_stop and ride.route_stop.meeting_point else None
        routes.append({**_route_info(ride.route), "as": "passenger", "meeting_point": stop})
    driven = (
        Route.objects.filter(Q(service_date__isnull=True) | Q(service_date__gte=today), vehicle__driver=person, is_active=True)
        .select_related("schedule", "vehicle")
    )
    for route in driven:
        routes.append({**_route_info(route), "as": "driver", "vehicle": f"{route.vehicle.model} {route.vehicle.plate_number}"})
    # Fixas primeiro (por horário), depois avulsas por data.
    routes.sort(key=lambda r: (r["service_date"] is not None, r["service_date"] or "", r["arrival"] or ""))

    return {
        "account": _account_info(person),
        "addresses": [address_payload(a) for a in person.addresses.filter(is_active=True).order_by("-created_at", "-id")],
        # Endereços anteriores (trocados pelo colaborador no app ou pelo painel), do mais recente ao mais antigo.
        "address_history": [
            address_payload(a) for a in person.addresses.filter(is_active=False).order_by("-created_at", "-id")[:5]
        ],
        "routes": routes,
        "linked_records": linked_records(person),
    }


def linked_records(person: Person) -> list:
    """Outros cadastros da mesma empresa com o mesmo nome e telefone (ex.: a mesma pessoa como motorista e funcionária)."""
    if not person.phone:
        return []
    twins = Person.objects.filter(company_id=person.company_id, phone=person.phone, name__iexact=person.name).exclude(pk=person.pk)
    return [
        {
            "id": twin.id, "name": twin.name, "type": twin.type, "is_active": twin.is_active,
            "code": twin.employee_code or twin.license_number,
        }
        for twin in twins.order_by("id")
    ]


def _route_info(route: Route) -> dict:
    schedule = route.schedule
    return {
        "id": route.id,
        "name": route.name,
        "arrival": schedule.time.strftime("%H:%M") if schedule else None,
        "weekdays": schedule.weekdays if schedule else [],
        "service_date": route.service_date.isoformat() if route.service_date else None,
    }


def _account_info(person: Person):
    try:
        account = person.access_account
    except AccessAccount.DoesNotExist:
        return None
    return {
        "email": account.email,
        "role": account.role,
        "must_change_password": account.must_change_password,
        "last_login": account.last_login.isoformat() if getattr(account, "last_login", None) else None,
    }
