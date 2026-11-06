"""Registro do embarque por dia (BoardingRecord), gravado sempre que o status muda."""
from django.utils import timezone

from .dates import local_today
from .models import BoardingRecord


def record_boarding(passenger, marked_by=None) -> BoardingRecord:
    """
    Guarda o status atual do passageiro como o embarque de hoje (fuso da operação).
    Mudou de novo no mesmo dia? Atualiza o mesmo registro: vale o último status do dia.
    """
    now = timezone.now()
    record, _ = BoardingRecord.objects.update_or_create(
        route_passenger=passenger,
        service_date=local_today(),
        defaults={
            "route_id": passenger.route_id,
            "person_id": passenger.person_id,
            "status": passenger.boarding_status,
            "marked_by": marked_by,
            "updated_at": now,
        },
        create_defaults={
            "route_id": passenger.route_id,
            "person_id": passenger.person_id,
            "status": passenger.boarding_status,
            "marked_by": marked_by,
            "created_at": now,
            "updated_at": now,
        },
    )
    return record
