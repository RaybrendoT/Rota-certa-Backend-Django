from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from corsheaders.defaults import default_methods
from decouple import config

BASE_DIR = Path(__file__).resolve().parent.parent


DEBUG = config('DEBUG', default=False, cast=bool)
SECRET_KEY = config('SECRET_KEY', default='insecure-key-change-me')
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1,0.0.0.0').split(',')
if DEBUG:
    # Para desenvolvimento local (Flutter emulador/device), evita erro de host inválido.
    ALLOWED_HOSTS = ["*"]



INSTALLED_APPS = [
    "daphne",  # runserver em ASGI (WebSocket); precisa vir antes de staticfiles
    "jazzmin",  # tema do admin; precisa vir antes de django.contrib.admin
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third party
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "corsheaders",
    "channels",
    # Apps
    "core",
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',  # só uma vez
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'core.middleware.AdminLocaleMiddleware',
]

ROOT_URLCONF = 'config.urls'


TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        # templates/ do projeto vem antes dos apps: é onde o admin do Jazzmin é ajustado.
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Channel layer do WebSocket: Redis quando REDIS_URL existir (obrigatório com mais de um
# processo/pod); sem ela, memória do processo (desenvolvimento e testes).
REDIS_URL = config('REDIS_URL', default='')
if REDIS_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            # socket_timeout acima da espera bloqueante do channels_redis (BZPOPMIN de 5 s):
            # com o padrão de 5 s do redis-py, toda leitura ociosa estoura e derruba o WebSocket.
            'CONFIG': {'hosts': [{'address': REDIS_URL, 'socket_timeout': 30}]},
        }
    }
else:
    CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}


def build_database_config(database_url: str):
    """
    PostgreSQL é o único motor suportado. Não existe fallback para SQLite:
    a ausência ou um esquema diferente de DATABASE_URL impede a subida da
    aplicação (fail fast) em vez de gravar silenciosamente num arquivo
    local descartável (ver database/04-postgres-exclusivo.md nas specs).
    """
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL não definida. Este projeto usa PostgreSQL "
            "exclusivamente — não há mais fallback para SQLite. "
            "Defina, por exemplo: "
            "postgres://usuario:senha@host:5432/nome_do_banco?sslmode=require"
        )

    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError(
            f"DATABASE_URL deve usar o esquema postgres:// ou postgresql:// "
            f"(recebido: {parsed.scheme!r}). SQLite não é suportado neste projeto."
        )

    query = parse_qs(parsed.query)

    options = {}
    sslmode = query.get("sslmode", [None])[0]
    if sslmode:
        options["sslmode"] = sslmode

    conn_max_age = query.get("conn_max_age", [None])[0]

    db_config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "localhost",
        "PORT": parsed.port or 5432,
        "CONN_MAX_AGE": int(conn_max_age) if conn_max_age else 60,
    }
    if options:
        db_config["OPTIONS"] = options

    return db_config


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases
DATABASES = {
    "default": build_database_config(config("DATABASE_URL", default=""))
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in config('CSRF_TRUSTED_ORIGINS', default='http://localhost:4200').split(',')
    if origin.strip()
]

# HTTPS atrás do Traefik (k3s): o TLS termina no proxy, que informa o esquema original.
# Desligado por padrão para o desenvolvimento local em HTTP; ligado no deploy (USE_HTTPS=True).
USE_HTTPS = config('USE_HTTPS', default=False, cast=bool)
if USE_HTTPS:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=0, cast=int)
    # O redirecionamento HTTP->HTTPS é do Traefik; aqui quebraria as probes HTTP internas.

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# config.asgi serve /static/ quando não há proxy/CDN na frente.
SERVE_STATIC_FILES = config('SERVE_STATIC_FILES', default=True, cast=bool)
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_METHODS = default_methods

REST_FRAMEWORK = {
"DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "core.authentication.AccessAccountJWTAuthentication",
    ],
    # Nada é público por padrão; os viewsets de cadastro exigem MANAGER (core.permissions).
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 10,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle'
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        # GPS do motorista (app envia a cada ~5 s); escopo próprio para não esgotar o 'user'.
        'vehicle_location': '120/min',
        # Mensagens ao assistente de planejamento (cada uma pode virar várias chamadas pagas ao Claude).
        'planning_message': '20/min',
        # Conferência de endereço pelo colaborador (cada uma consulta a Geocoding API do Google).
        'address_lookup': '30/hour',
    },
    # Atrás de proxy reverso (Traefik no k3s) o IP real vem em X-Forwarded-For; sem isso
    # todos os clientes dividem o mesmo contador de throttle. Defina NUM_PROXIES=1 no deploy.
    'NUM_PROXIES': config('NUM_PROXIES', default='', cast=lambda v: int(v) if str(v).strip() else None),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "access_account_id",
    "AUTH_TOKEN_CLASSES": (
        "core.tokens.AccessAccountAccessToken",
        "core.tokens.AccessAccountRefreshToken",
    ),
    "CHECK_REVOKE_TOKEN": False,
    "UPDATE_LAST_LOGIN": False,
}

ACCESS_ACCOUNT_MAX_FAILED_ATTEMPTS = 5
ACCESS_ACCOUNT_LOCKOUT_MINUTES = 30

ACCESS_ACCOUNT_AUTO_CREATE = config(
    "ACCESS_ACCOUNT_AUTO_CREATE", default=True, cast=bool
)
ACCESS_ACCOUNT_AUTO_INITIAL_PASSWORD = config(
    "ACCESS_ACCOUNT_AUTO_INITIAL_PASSWORD",
    default="",
)
ACCESS_ACCOUNT_SYNC_EMAIL = config(
    "ACCESS_ACCOUNT_SYNC_EMAIL",
    default=True,
    cast=bool,
)

PASSWORD_RESET_CODE_TTL_MINUTES = config(
    "PASSWORD_RESET_CODE_TTL_MINUTES",
    default=15,
    cast=int,
)
PASSWORD_RESET_CODE_LENGTH = config(
    "PASSWORD_RESET_CODE_LENGTH",
    default=6,
    cast=int,
)
PASSWORD_RESET_RETURN_CODE_IN_RESPONSE = config(
    "PASSWORD_RESET_RETURN_CODE_IN_RESPONSE",
    default=DEBUG,
    cast=bool,
)
PASSWORD_RESET_FORCE_MUST_CHANGE_PASSWORD = config(
    "PASSWORD_RESET_FORCE_MUST_CHANGE_PASSWORD",
    default=False,
    cast=bool,
)

GOOGLE_MAPS_API_KEY = config("GOOGLE_MAPS_API_KEY", default="")
GOOGLE_ROUTES_API_URL = config(
    "GOOGLE_ROUTES_API_URL",
    default="https://routes.googleapis.com/directions/v2:computeRoutes",
)

# Assistente de planejamento de rotas (Claude). Sem chave, o endpoint responde 503.
ANTHROPIC_API_KEY = config("ANTHROPIC_API_KEY", default="")
PLANNING_MODEL = config("PLANNING_MODEL", default="claude-opus-5-5")

# Notificações push (Firebase Cloud Messaging, API HTTP v1). Ver core/push.py.
# Use um dos dois: caminho do JSON da conta de serviço ou o próprio JSON (texto ou base64).
# Sem nenhum, o push fica desligado.
FCM_SERVICE_ACCOUNT_FILE = config("FCM_SERVICE_ACCOUNT_FILE", default="")
FCM_SERVICE_ACCOUNT_JSON = config("FCM_SERVICE_ACCOUNT_JSON", default="")
FCM_PROJECT_ID = config("FCM_PROJECT_ID", default="")  # vazio = project_id da conta de serviço
FCM_ANDROID_CHANNEL_ID = config("FCM_ANDROID_CHANNEL_ID", default="avisos_da_van")
FCM_HTTP_TIMEOUT = config("FCM_HTTP_TIMEOUT", default=15, cast=int)
# Envia numa thread depois do commit; False envia na hora (testes).
PUSH_ASYNC = config("PUSH_ASYNC", default=True, cast=bool)

# ====================== ADMIN (Django admin + Jazzmin) ======================
# Idioma e fuso só do /admin (core.middleware.AdminLocaleMiddleware); a API segue en-us/UTC.
ADMIN_LANGUAGE_CODE = "pt-br"
ADMIN_TIME_ZONE = config("ADMIN_TIME_ZONE", default="America/Manaus")
# Para onde o link "Painel Rota Certa" do admin leva (no k3s é a mesma origem).
FRONTEND_URL = config("FRONTEND_URL", default="http://localhost:4200/")

JAZZMIN_SETTINGS = {
    "site_title": "Rota Certa · Admin",
    "site_header": "Rota Certa",
    "site_brand": "Rotacerta",
    "site_logo": "core/img/logo.svg",
    "login_logo": "core/img/logo.svg",
    "site_logo_classes": "",
    "site_icon": "core/img/logo.svg",
    "welcome_sign": "Administração do Rota Certa",
    "copyright": "Rota Certa",
    "search_model": ["core.Person"],
    "topmenu_links": [
        {"name": "Início", "url": "admin:index"},
        {"name": "Painel Rota Certa", "url": FRONTEND_URL, "new_window": True},
    ],
    "usermenu_links": [
        {"name": "Painel Rota Certa", "url": FRONTEND_URL, "new_window": True, "icon": "fas fa-external-link-alt"},
    ],
    "show_sidebar": True,
    "navigation_expanded": True,
    "order_with_respect_to": [
        "core",
        "core.company",
        "core.person",
        "core.accessaccount",
        "core.vehicle",
        "core.route",
        "core.routestop",
        "core.routepassenger",
        "core.meetingpoint",
        "core.address",
        "core.schedule",
        "core.routeevent",
        "auth",
    ],
    "icons": {
        "auth": "fas fa-user-shield",
        "auth.user": "fas fa-user-shield",
        "auth.Group": "fas fa-users-cog",
        "core.Company": "fas fa-building",
        "core.Person": "fas fa-users",
        "core.AccessAccount": "fas fa-id-badge",
        "core.Vehicle": "fas fa-bus",
        "core.Route": "fas fa-route",
        "core.RouteStop": "fas fa-map-pin",
        "core.RoutePassenger": "fas fa-user-check",
        "core.MeetingPoint": "fas fa-map-marker-alt",
        "core.Address": "fas fa-map-marked-alt",
        "core.Schedule": "fas fa-clock",
        "core.RouteEvent": "fas fa-stream",
    },
    "default_icon_parents": "fas fa-folder",
    "default_icon_children": "fas fa-circle",
    "related_modal_active": True,
    "custom_css": "core/admin/rotacerta.css",
    "custom_js": "core/admin/rotacerta.js",
    "show_ui_builder": False,
    "changeform_format": "horizontal_tabs",
    "changeform_format_overrides": {"auth.user": "collapsible", "auth.group": "vertical_tabs"},
    "language_chooser": False,
}

# Cores do painel Angular: topo azul (#1e40af), menu escuro (#0f172a), destaque #3b82f6.
JAZZMIN_UI_TWEAKS = {
    "theme": "default",
    "navbar": "navbar-primary navbar-dark",
    "brand_colour": "navbar-primary",
    "accent": "accent-primary",
    "sidebar": "sidebar-dark-primary",
    "sidebar_nav_flat_style": True,
    "sidebar_nav_child_indent": True,
    "navbar_fixed": True,
    "sidebar_fixed": True,
    "button_classes": {
        "primary": "btn-primary",
        "secondary": "btn-outline-secondary",
        "info": "btn-info",
        "warning": "btn-warning",
        "danger": "btn-danger",
        "success": "btn-success",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Route-monitoring API",
    "DESCRIPTION": "API for routes management system",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
