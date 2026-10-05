from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone, translation


class AdminLocaleMiddleware:
    """
    Liga português e o fuso local só nas páginas do /admin/.

    A API continua em LANGUAGE_CODE/TIME_ZONE (en-us/UTC): o app e o painel dependem das
    mensagens e datas como estão. Sem LocaleMiddleware a tradução ativada ficaria presa à
    thread, então ela é sempre desfeita ao fim da requisição.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith("/admin/"):
            return self.get_response(request)

        translation.activate(settings.ADMIN_LANGUAGE_CODE)
        request.LANGUAGE_CODE = translation.get_language()
        timezone.activate(ZoneInfo(settings.ADMIN_TIME_ZONE))
        try:
            response = self.get_response(request)
            response.headers.setdefault("Content-Language", request.LANGUAGE_CODE)
            return response
        finally:
            translation.deactivate()
            timezone.deactivate()
