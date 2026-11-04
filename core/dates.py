from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def local_today():
    """Data de hoje no fuso da operação (Manaus); o banco trabalha em UTC."""
    return timezone.now().astimezone(ZoneInfo(settings.ADMIN_TIME_ZONE)).date()
