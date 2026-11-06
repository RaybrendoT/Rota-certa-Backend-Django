"""
Previsão de chegada do ônibus ao ponto de cada passageiro, pelo trajeto das ruas.

O trajeto da rota (paradas em ordem + empresa) vem de core.road_path: pelas ruas quando há
chave do Google, senão a linha reta entre as paradas. A posição do ônibus é projetada nesse
trajeto; o progresso fica guardado em VehicleLocation.progress_meters para não "pular" de
trecho quando o trajeto passa duas vezes pela mesma rua.

ETA = distância restante pelo trajeto / max(velocidade média, velocidade do GPS)
      + tempo parado em cada parada antes da do passageiro.
"""
import hashlib
import math
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from .models import Address
from .road_path import SOURCE_STRAIGHT, encode_polyline, get_road_path

EARTH_RADIUS_M = 6_371_000
# Distância (m) a partir da qual uma parada conta como "já passou" pelo ônibus.
REACHED_TOLERANCE_M = 30


class Phase:
    WAITING = "waiting"  # sem posição ao vivo (rota não iniciada ou GPS parado)
    APPROACHING = "approaching"  # a caminho do ponto do passageiro
    ARRIVING = "arriving"  # a poucos metros do ponto
    PASSED = "passed"  # já passou do ponto (a caminho da empresa)
    AT_DESTINATION = "at_destination"


def _setting(name, default):
    return getattr(settings, name, default)


def haversine_m(a, b) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def company_destination(route):
    """Endereço da empresa: endereço da empresa que não é de pessoa nem de ponto de encontro."""
    return (
        Address.objects.filter(is_active=True, person__isnull=True, company=route.company)
        .exclude(meeting_points__isnull=False)
        .order_by("id")
        .first()
    )


@dataclass
class PathPoint:
    lat: float
    lng: float
    stop_id: int | None = None  # RouteStop.id, ou None


def route_waypoints(route):
    """Paradas em ordem + empresa no final (linha reta)."""
    stops = route.stops.filter(is_active=True).select_related("meeting_point__address").order_by("sequence")
    points = [
        PathPoint(float(s.meeting_point.address.latitude), float(s.meeting_point.address.longitude), s.id)
        for s in stops
    ]
    dest = company_destination(route)
    if dest is not None:
        points.append(PathPoint(float(dest.latitude), float(dest.longitude), None))
    return points


def route_path_with_source(route):
    """Trajeto usado nos cálculos e sua origem: pelas ruas ("google") ou linha reta entre as paradas."""
    waypoints = route_waypoints(route)
    road = get_road_path(route, [(p.lat, p.lng) for p in waypoints])
    if road is None:
        return waypoints, SOURCE_STRAIGHT
    path = [PathPoint(lat, lng) for lat, lng in road["points"]]
    for waypoint, index in zip(waypoints, road["stop_indexes"]):
        if waypoint.stop_id is not None and 0 <= index < len(path):
            path[index].stop_id = waypoint.stop_id
    return path, road["source"]


def route_path(route):
    return route_path_with_source(route)[0]


def path_payload(route, path=None, source=None):
    """Trajeto para o app desenhar (polyline do Google, precisão 5), sua origem e versão."""
    if path is None:
        path, source = route_path_with_source(route)
    raw = ";".join(f"{p.lat:.5f},{p.lng:.5f}" for p in path)
    return {
        "polyline": encode_polyline([(p.lat, p.lng) for p in path]),
        "source": source,
        "version": hashlib.sha1(raw.encode()).hexdigest()[:12],
    }


def cumulative_distances(path):
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + haversine_m((a.lat, a.lng), (b.lat, b.lng)))
    return cum


def locate_on_path(point, path, cum, previous_along=None):
    """
    Distância percorrida (m) pelo ônibus no trajeto. Entre os trechos próximos do ponto,
    prefere o que continua a partir da posição anterior, para não pular de trecho quando
    o trajeto se cruza.
    """
    if len(path) < 2:
        return 0.0
    lat0 = math.radians(point[0])
    kx = math.cos(lat0) * math.pi / 180 * EARTH_RADIUS_M
    ky = math.pi / 180 * EARTH_RADIUS_M
    px, py = point[1] * kx, point[0] * ky
    candidates = []
    for i, (a, b) in enumerate(zip(path, path[1:])):
        ax, ay, bx, by = a.lng * kx, a.lat * ky, b.lng * kx, b.lat * ky
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        off = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        candidates.append((cum[i] + t * (cum[i + 1] - cum[i]), off))
    min_off = min(c[1] for c in candidates)
    near = [c for c in candidates if c[1] <= min_off + 40]
    if previous_along is None:
        return min(near, key=lambda c: c[1])[0]
    ahead = [c for c in near if c[0] >= previous_along - 50] or near
    return min(ahead, key=lambda c: abs(c[0] - previous_along))[0]


def update_progress(location, previous_along=None):
    """Calcula e grava o progresso do ônibus no trajeto (chamado a cada posição recebida)."""
    path = route_path(location.route)
    if len(path) < 2:
        return None
    along = locate_on_path((float(location.latitude), float(location.longitude)), path, cumulative_distances(path),
                           previous_along)
    type(location).objects.filter(pk=location.pk).update(progress_meters=along)
    location.progress_meters = along
    return along


def estimate(passenger, location=None, path=None, now=None):
    """
    Situação do ônibus em relação ao ponto do passageiro (RoutePassenger):
    {"phase", "is_live", "minutes", "seconds", "distance_meters", "stops_before", "age_seconds"}.
    """
    now = now or timezone.now()
    route = passenger.route
    if location is None:
        location = getattr(route, "current_location", None)
    result = {"phase": Phase.WAITING, "is_live": False, "minutes": None, "seconds": None,
              "distance_meters": None, "stops_before": None, "age_seconds": None}
    if location is None:
        return result
    age = max(0, int((now - location.recorded_at).total_seconds()))
    result["age_seconds"] = age
    if age > _setting("ETA_LOCATION_MAX_AGE_SECONDS", 120):
        return result
    result["is_live"] = True

    path = path if path is not None else route_path(route)
    if len(path) < 2:
        result["phase"] = Phase.APPROACHING
        return result
    cum = cumulative_distances(path)
    along = location.progress_meters
    if along is None:
        along = locate_on_path((float(location.latitude), float(location.longitude)), path, cum)

    if path[-1].stop_id is None and cum[-1] - along <= _setting("ETA_DESTINATION_METERS", 150):
        result["phase"] = Phase.AT_DESTINATION
        return result
    my_index = next((j for j, p in enumerate(path) if p.stop_id is not None and p.stop_id == passenger.route_stop_id),
                    None)
    if my_index is None:
        result["phase"] = Phase.APPROACHING
        return result

    remaining = cum[my_index] - along
    if remaining < -REACHED_TOLERANCE_M * 3:
        result["phase"] = Phase.PASSED
        return result
    stops_before = sum(1 for j, p in enumerate(path)
                       if p.stop_id is not None and j < my_index and cum[j] > along + REACHED_TOLERANCE_M)
    speed_mps = max(_setting("ETA_AVERAGE_SPEED_KMH", 25) / 3.6, location.speed or 0)
    seconds = max(0.0, remaining) / speed_mps + stops_before * _setting("ETA_STOP_DWELL_SECONDS", 60)
    arrived = remaining <= REACHED_TOLERANCE_M
    result.update(
        phase=Phase.ARRIVING if remaining <= _setting("ETA_ARRIVING_METERS", 300) else Phase.APPROACHING,
        seconds=0 if arrived else round(seconds),
        minutes=0 if arrived else max(1, math.ceil(seconds / 60)),
        distance_meters=max(0, round(remaining)),
        stops_before=stops_before,
    )
    return result


def passenger_payload(passenger):
    """Previsão e trajeto da rota do passageiro, para o app do colaborador."""
    path, source = route_path_with_source(passenger.route)
    return {"eta": estimate(passenger, path=path), "path": path_payload(passenger.route, path, source)}
