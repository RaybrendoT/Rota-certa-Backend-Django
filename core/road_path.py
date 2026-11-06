"""
Trajeto da rota pelas ruas.

O caminho de carro "parada 1 → parada 2 → … → empresa" é pedido à Google Routes API
uma única vez e guardado em `Route.road_path`, junto com uma assinatura das
coordenadas das paradas. Enquanto as paradas não mudam, nenhuma nova chamada é feita.
Sem chave (GOOGLE_MAPS_API_KEY) ou se a API falhar, o trajeto é a linha reta entre
as paradas (comportamento anterior).
"""
import hashlib
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

SOURCE_GOOGLE = "google"
SOURCE_STRAIGHT = "straight"
FAILURE_RETRY_SECONDS = 600


# ---------------------------------------------------------------- polyline (formato do Google, precisão 5)

def encode_polyline(points, precision=5):
    factor = 10 ** precision
    out = []
    prev_lat = prev_lng = 0
    for lat, lng in points:
        ilat, ilng = round(lat * factor), round(lng * factor)
        for value in (ilat - prev_lat, ilng - prev_lng):
            value = ~(value << 1) if value < 0 else value << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        prev_lat, prev_lng = ilat, ilng
    return "".join(out)


def decode_polyline(text, precision=5):
    factor = 10 ** precision
    points, index, lat, lng = [], 0, 0, 0
    while index < len(text):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(text[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat += deltas[0]
        lng += deltas[1]
        points.append((lat / factor, lng / factor))
    return points


# ---------------------------------------------------------------- cálculo e cache

def waypoints_signature(waypoints):
    raw = ";".join(f"{lat:.6f},{lng:.6f}" for lat, lng in waypoints)
    return hashlib.sha1(raw.encode()).hexdigest()


def _fetch_google(waypoints):
    """Chama a Routes API (sem trânsito: SKU mais barato) e devolve pontos + índices das paradas."""
    def latlng(p):
        return {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}}

    payload = {
        "origin": latlng(waypoints[0]),
        "destination": latlng(waypoints[-1]),
        "intermediates": [latlng(p) for p in waypoints[1:-1]],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
        "computeAlternativeRoutes": False,
        "languageCode": "pt-BR",
        "units": "METRIC",
    }
    request = urllib.request.Request(
        settings.GOOGLE_ROUTES_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
            "X-Goog-FieldMask": "routes.distanceMeters,routes.legs.distanceMeters,routes.legs.polyline.encodedPolyline",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    route = (data.get("routes") or [None])[0]
    if not route or len(route.get("legs") or []) != len(waypoints) - 1:
        raise ValueError(f"Resposta inesperada da Routes API: {str(data)[:200]}")

    points, stop_indexes = [], [0]
    for leg in route["legs"]:
        leg_points = decode_polyline(leg["polyline"]["encodedPolyline"]) or []
        if points and leg_points and leg_points[0] == points[-1]:
            leg_points = leg_points[1:]
        points.extend(leg_points)
        stop_indexes.append(max(len(points) - 1, 0))
    if len(points) < 2:
        raise ValueError("Trajeto vazio na resposta da Routes API.")
    return points, stop_indexes, route.get("distanceMeters")


# Motivos de bloqueio mais comuns da chave, com a correção no Google Cloud.
_REASON_HINTS = {
    "API_KEY_SERVICE_BLOCKED": "a chave não tem a Routes API liberada em 'Restrições de API'",
    "API_KEY_ANDROID_APP_BLOCKED": "a chave está restrita a 'Apps Android'; use 'Nenhum' ou 'Endereços IP'",
    "API_KEY_HTTP_REFERRER_BLOCKED": "a chave está restrita a 'Sites'; use 'Nenhum' ou 'Endereços IP'",
    "API_KEY_IP_ADDRESS_BLOCKED": "o IP deste servidor não está na lista de 'Endereços IP' da chave",
    "API_KEY_INVALID": "a chave está errada ou foi apagada/regenerada",
    "SERVICE_DISABLED": "a Routes API não está ativada no projeto",
    "BILLING_DISABLED": "o projeto não tem faturamento ativo",
}


def _http_error_detail(exc):
    body = exc.read().decode("utf-8", "ignore")
    try:
        error = json.loads(body)["error"]
    except (ValueError, KeyError, TypeError):
        return body[:300]
    reasons = [d.get("reason") for d in error.get("details") or [] if d.get("reason")]
    detail = f"{error.get('status', exc.code)}: {error.get('message', '')}"
    for reason in reasons:
        detail += f" [{reason}]"
        if reason in _REASON_HINTS:
            detail += f" -> {_REASON_HINTS[reason]}"
    return detail


def get_road_path(route, waypoints, force=False):
    """
    Trajeto pelas ruas para os `waypoints` [(lat, lng), ...] da rota (paradas + empresa).
    Retorna {"points": [...], "stop_indexes": [...], "source": "google"} ou None
    (sem chave, desligado ou API indisponível → o chamador usa a linha reta).
    """
    if len(waypoints) < 2:
        return None
    signature = waypoints_signature(waypoints)
    stored = route.road_path or {}
    if not force and stored.get("signature") == signature and stored.get("polyline"):
        return {"points": decode_polyline(stored["polyline"]), "stop_indexes": stored["stop_indexes"],
                "source": stored.get("source", SOURCE_GOOGLE)}

    if not getattr(settings, "ROAD_PATH_ENABLED", True) or not settings.GOOGLE_MAPS_API_KEY:
        return None
    failure_key = f"road_path_failure:{route.pk}:{signature}"
    if not force and cache.get(failure_key):
        return None
    try:
        points, stop_indexes, distance = _fetch_google(waypoints)
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as exc:
        detail = _http_error_detail(exc) if isinstance(exc, urllib.error.HTTPError) else exc
        logger.warning("Routes API falhou para a rota %s: %s", route.pk, detail)
        cache.set(failure_key, True, FAILURE_RETRY_SECONDS)
        return None

    route.road_path = {
        "signature": signature,
        "source": SOURCE_GOOGLE,
        "polyline": encode_polyline(points),
        "stop_indexes": stop_indexes,
        "distance_meters": distance,
        "computed_at": timezone.now().isoformat(),
    }
    type(route).objects.filter(pk=route.pk).update(road_path=route.road_path)
    return {"points": points, "stop_indexes": stop_indexes, "source": SOURCE_GOOGLE}
