import math

EARTH_RADIUS_METERS = 6_371_000.0


def distance_meters(lat1, lon1, lat2, lon2) -> float:
    """Distância em metros entre dois pontos (fórmula de haversine)."""
    lat1, lon1, lat2, lon2 = (float(v) for v in (lat1, lon1, lat2, lon2))
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return EARTH_RADIUS_METERS * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
