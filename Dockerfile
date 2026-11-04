FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt gunicorn==23.0.0 whitenoise==6.9.0

COPY . .

# collectstatic só precisa que as settings carreguem; os valores abaixo valem apenas no build.
RUN SECRET_KEY=build-only DATABASE_URL=postgres://u:p@localhost:5432/db \
    python manage.py collectstatic --noinput

RUN useradd --system --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000
# ASGI (daphne): HTTP + WebSocket (/ws/stream/). Com mais de uma réplica, defina REDIS_URL.
# Aplica as migrações a cada subida (no deploy automático, a versão nova migra o banco).
# --proxy-headers: atrás do Caddy, o IP do cliente vem do X-Forwarded-For.
# MIGRATE_ON_START=0 desliga a migração (ex.: para rodar várias réplicas).
CMD ["sh", "-c", "if [ \"${MIGRATE_ON_START:-1}\" = 1 ]; then python manage.py migrate --noinput; fi && exec daphne -b 0.0.0.0 -p 8000 --proxy-headers config.asgi:application"]
