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
CMD ["gunicorn", "config.wsgi_prod:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--access-logfile", "-"]
