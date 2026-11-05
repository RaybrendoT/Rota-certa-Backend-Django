"""
WSGI de produção: a aplicação Django + arquivos estáticos (admin, Swagger) servidos pelo
WhiteNoise, para o container não depender de um nginx só para /static/.

Uso: gunicorn config.wsgi_prod:application
"""
from django.conf import settings
from whitenoise import WhiteNoise

from config.wsgi import application as django_application

application = WhiteNoise(
    django_application,
    root=settings.STATIC_ROOT,
    prefix=settings.STATIC_URL,
)
