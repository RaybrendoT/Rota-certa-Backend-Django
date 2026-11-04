"""
Endereço residencial informado pelo próprio colaborador no app (localização atual ou digitado).

O app só coleta e mostra; toda regra que garante um endereço válido roda aqui, e de novo na hora de
gravar (a prévia não é confiável por si só):
- GPS: leitura nova com precisão de até 50 m e sem localização simulada;
- digitado: o Google precisa achar o endereço, e o CEP precisa bater com o encontrado;
- o alfinete ajustado no mapa fica a até 300 m do endereço encontrado (ou da leitura do GPS);
- o ponto fica a até 60 km da empresa;
- até 3 trocas por semana.
Ao gravar, o endereço fica pendente (inativo) até o gestor aprovar em core.address_review; até lá vale
o anterior. Um novo envio substitui o pedido pendente anterior. Os gestores da empresa são avisados.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.geo import distance_meters
from core.geocoding import geocode_address, only_digits, reverse_geocode
from core.models import AccessAccount, Address, MeetingPoint, Notification, Person
from core.notifications import notify

MAX_GPS_ACCURACY_M = 50
MAX_PIN_SHIFT_M = 300
MAX_DISTANCE_FROM_COMPANY_M = 60_000
MAX_CHANGES_PER_WEEK = 3
SELF_SOURCES = (Address.Source.GPS, Address.Source.TYPED)


class AddressRejected(Exception):
    """Endereço que não passa nas regras; a mensagem vai para o colaborador."""


class TooManyChanges(AddressRejected):
    pass


def company_location(company):
    """Endereço da empresa (destino das rotas): o da empresa que não é ponto de encontro."""
    stop_addresses = MeetingPoint.objects.filter(company=company).values("address_id")
    return (
        Address.objects.filter(company=company, person__isnull=True, is_active=True)
        .exclude(id__in=stop_addresses)
        .order_by("id")
        .first()
    )


def current_address(person: Person) -> dict:
    address = person.addresses.filter(is_active=True).order_by("-created_at", "-id").first()
    pending = person.addresses.filter(review_status=Address.Review.PENDING).order_by("-created_at", "-id").first()
    # Recusa mais recente, só enquanto não houve envio depois dela (o app mostra o motivo).
    last_self = person.addresses.filter(source__in=SELF_SOURCES).order_by("-created_at", "-id").first()
    rejected = last_self if last_self and last_self.review_status == Address.Review.REJECTED else None
    return {
        "address": address_payload(address) if address else None,
        "nearest_point": nearest_point(person.company, address.latitude, address.longitude) if address else None,
        "pending": address_payload(pending) if pending else None,
        "rejected": address_payload(rejected) if rejected else None,
        "changes_left_this_week": max(MAX_CHANGES_PER_WEEK - _recent_changes(person), 0),
    }


def preview(person: Person, data: dict) -> dict:
    """Confere a localização ou o endereço digitado e devolve o endereço normalizado; não grava nada."""
    warnings = []
    if data["mode"] == "gps":
        lat, lng = _check_gps(data)
        _check_area(person.company, lat, lng)
        found = reverse_geocode(lat, lng)
        if found is None:
            warnings.append("Não achamos o nome da rua para essa localização; preencha o endereço.")
            found = {}
        address = {
            "street": found.get("street", ""),
            "number": found.get("number", ""),
            "neighborhood": found.get("sublocality", ""),
            "city": found.get("city", ""),
            "state": found.get("state", ""),
            "postal_code": _format_cep(found.get("postal_code", "")),
        }
        return {
            "source": Address.Source.GPS,
            "latitude": lat, "longitude": lng, "accuracy_m": data["accuracy"],
            "exact": True, "address": address, "warnings": warnings,
            "nearest_point": nearest_point(person.company, lat, lng),
        }

    found, cep_warning = _geocode_typed(data)
    if cep_warning:
        warnings.append(cep_warning)
    if not found["exact"]:
        warnings.append("Achamos só a rua ou o bairro: arraste o alfinete até a sua casa antes de confirmar.")
    _check_area(person.company, found["latitude"], found["longitude"])
    return {
        "source": Address.Source.TYPED,
        "latitude": found["latitude"], "longitude": found["longitude"], "accuracy_m": None,
        "exact": found["exact"],
        "address": {
            "street": found["street"] or data["street"],
            "number": data["number"],
            "neighborhood": found["sublocality"] or data.get("neighborhood", ""),
            "city": found["city"] or data["city"],
            "state": found["state"] or data["state"],
            "postal_code": _format_cep(data["postal_code"]),
        },
        "warnings": warnings,
        "nearest_point": nearest_point(person.company, found["latitude"], found["longitude"]),
    }


def save(person: Person, data: dict) -> dict:
    """Valida tudo de novo e grava o endereço confirmado (com o alfinete na posição final)."""
    if _recent_changes(person) >= MAX_CHANGES_PER_WEEK:
        raise TooManyChanges(
            f"Você já atualizou o endereço {MAX_CHANGES_PER_WEEK} vezes nesta semana. "
            "Se precisar corrigir, fale com o gestor de transporte."
        )
    source = data["source"]
    if source == Address.Source.GPS:
        anchor = _check_gps({
            "latitude": data.get("gps_latitude"), "longitude": data.get("gps_longitude"),
            "accuracy": data.get("gps_accuracy"), "is_mock": data.get("gps_is_mock", False),
        })
        precision, accuracy = "gps", data.get("gps_accuracy")
    else:
        found, _ = _geocode_typed(data)
        anchor = (found["latitude"], found["longitude"])
        precision, accuracy = ("exact" if found["exact"] else "approximate"), None

    lat, lng = float(data["latitude"]), float(data["longitude"])
    shift = distance_meters(lat, lng, *anchor)
    if shift > MAX_PIN_SHIFT_M:
        raise AddressRejected(
            f"O alfinete ficou a {shift:.0f} m do endereço encontrado (máximo {MAX_PIN_SHIFT_M} m). "
            "Confira o endereço ou use a localização atual estando em casa."
        )
    _check_area(person.company, lat, lng)

    with transaction.atomic():
        # Só um pedido pendente por pessoa: o novo envio substitui o anterior.
        person.addresses.filter(review_status=Address.Review.PENDING).update(
            review_status=Address.Review.REJECTED,
            review_note="Substituído por um novo envio.",
            reviewed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        address = Address.objects.create(
            person=person,
            street=data["street"].strip(),
            number=data["number"].strip(),
            complement=data.get("complement", "").strip(),
            neighborhood=data.get("neighborhood", "").strip(),
            city=data["city"].strip(),
            state=data["state"].strip().upper(),
            zip_code=_format_cep(data["postal_code"]),
            latitude=round(lat, 6),
            longitude=round(lng, 6),
            source=source,
            accuracy_m=accuracy,
            precision=precision,
            created_by=person,
            is_active=False,
            review_status=Address.Review.PENDING,
        )
        new_point = nearest_point(person.company, address.latitude, address.longitude)
        _notify_managers(person, address, new_point)

    return {**current_address(person), "message": _saved_message(new_point)}


def nearest_point(company, lat, lng):
    points = [
        mp for mp in MeetingPoint.objects.filter(company=company, is_active=True, home_of__isnull=True).select_related("address")
        if mp.address.latitude is not None
    ]
    if not points:
        return None
    best = min(points, key=lambda mp: distance_meters(lat, lng, mp.address.latitude, mp.address.longitude))
    return {
        "id": best.id,
        "name": best.name,
        "distance_m": round(distance_meters(lat, lng, best.address.latitude, best.address.longitude)),
    }


def address_payload(address: Address) -> dict:
    return {
        "id": address.id,
        "street": address.street,
        "number": address.number,
        "complement": address.complement,
        "neighborhood": address.neighborhood,
        "city": address.city,
        "state": address.state,
        "postal_code": address.zip_code,
        "latitude": float(address.latitude),
        "longitude": float(address.longitude),
        "source": address.source,
        "source_label": address.get_source_display(),
        "precision": address.precision,
        "accuracy_m": address.accuracy_m,
        "is_active": address.is_active,
        "review_status": address.review_status,
        "review_note": address.review_note,
        "reviewed_at": address.reviewed_at.isoformat() if address.reviewed_at else None,
        "created_at": address.created_at.isoformat(),
    }


# ---------------------------------------------------------------------- regras


def _check_gps(reading):
    if reading.get("is_mock"):
        raise AddressRejected("A localização veio de um app de localização simulada. Desative-o e tente de novo.")
    lat, lng, accuracy = reading.get("latitude"), reading.get("longitude"), reading.get("accuracy")
    if lat is None or lng is None or accuracy is None:
        raise AddressRejected("Leitura do GPS incompleta; tente de novo.")
    if accuracy > MAX_GPS_ACCURACY_M:
        raise AddressRejected(
            f"Sinal de GPS fraco (precisão de {accuracy:.0f} m; o mínimo é {MAX_GPS_ACCURACY_M} m). "
            "Vá para perto de uma janela ou digite o endereço."
        )
    return float(lat), float(lng)


def _geocode_typed(data):
    """(resultado do Google, aviso de CEP ou None) para o endereço digitado."""
    cep = only_digits(data.get("postal_code"))
    if len(cep) != 8:
        raise AddressRejected("Informe um CEP com 8 dígitos.")
    found = geocode_address(
        data["street"], data["number"], data.get("neighborhood", ""), data["city"], data["state"], cep
    )
    if found is None:
        raise AddressRejected("Não encontramos esse endereço. Confira rua, número, cidade e CEP.")
    found_cep = only_digits(found.get("postal_code"))
    if found_cep and found_cep[:5] != cep[:5]:
        raise AddressRejected(
            f"O CEP {_format_cep(cep)} não corresponde ao endereço encontrado ({_format_cep(found_cep)}). Confira o CEP."
        )
    warning = None
    if found_cep and found_cep != cep:
        warning = f"O CEP encontrado para esse endereço é {_format_cep(found_cep)}; confira o seu."
    return found, warning


def _check_area(company, lat, lng):
    location = company_location(company)
    if location is None:
        return
    meters = distance_meters(lat, lng, location.latitude, location.longitude)
    if meters > MAX_DISTANCE_FROM_COMPANY_M:
        raise AddressRejected(
            f"Esse endereço fica a {meters / 1000:.0f} km da empresa, fora da área atendida pelo transporte. "
            "Confira o endereço."
        )


def _recent_changes(person):
    since = timezone.now() - timedelta(days=7)
    return Address.objects.filter(person=person, source__in=SELF_SOURCES, created_at__gte=since).count()


def _format_cep(text):
    digits = only_digits(text)
    return f"{digits[:5]}-{digits[5:]}" if len(digits) == 8 else str(text or "")


def _saved_message(point):
    message = "Endereço enviado para aprovação do gestor de transporte. Até lá vale o endereço anterior."
    if point is None:
        return message
    return f"{message} Ponto de embarque mais próximo do novo endereço: {point['name']}, a {point['distance_m']} m."


def company_managers(company, exclude=None):
    managers = Person.objects.filter(company=company, is_active=True, access_account__role=AccessAccount.Role.MANAGER)
    return managers.exclude(pk=exclude.pk) if exclude else managers


def _notify_managers(person, address, new_point):
    managers = company_managers(person.company, exclude=person)
    if not managers:
        return
    how = "pela localização atual do celular" if address.source == Address.Source.GPS else "digitando no app"
    message = (
        f"{person.name} enviou um novo endereço {how}: {address.street}, {address.number} – "
        f"{address.neighborhood or address.city}."
    )
    if new_point:
        message += f" Ponto mais próximo: {new_point['name']} ({new_point['distance_m']} m)."
    message += " Aprove ou recuse em Endereços no painel."
    notify(
        managers,
        kind=Notification.Kind.ADDRESS_CHANGED,
        title="Novo endereço para aprovar",
        message=message,
        data={"person_id": person.id, "address_id": address.id},
    )
