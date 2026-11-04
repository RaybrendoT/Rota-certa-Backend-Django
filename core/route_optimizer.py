"""
Roteirização de transporte de funcionários até a empresa (depósito único).

Usado pelo seed da empresa simulada e pelo assistente de planejamento:
1. Varredura (sweep): pontos ordenados pelo ângulo em relação à empresa e agrupados em
   fatias vizinhas, respeitando lotação e número de paradas.
2. Inserção mais barata: dentro de cada rota, o ônibus começa longe da empresa e cada
   parada entra onde menos aumenta o trajeto até a empresa.

Um "item" é uma tupla (stop, riders): stop tem .lat/.lng e riders é a lista de passageiros
daquele ponto. O depósito (empresa) também só precisa de .lat/.lng.
"""
import math

from core.geo import distance_meters

# Fator de sinuosidade das ruas em relação à linha reta e velocidade média no trânsito de Manaus
# no horário de pico, para estimar duração sem chamar a Routes API.
ROAD_FACTOR = 1.35
AVERAGE_SPEED_KMH = 25
MINUTES_PER_STOP = 2


def _dist(a, b):
    return distance_meters(a.lat, a.lng, b.lat, b.lng)


def angle_from(depot, lat, lng):
    x = (lng - depot.lng) * math.cos(math.radians(depot.lat))
    y = lat - depot.lat
    return math.atan2(y, x)


def angular_distance(a, b):
    diff = abs(a - b) % (2 * math.pi)
    return min(diff, 2 * math.pi - diff)


def sweep_order(stops, depot):
    """Ordena por ângulo começando logo depois da maior lacuna, para nenhuma fatia atravessar o vazio."""
    ordered = sorted(stops, key=lambda s: angle_from(depot, s.lat, s.lng))
    if len(ordered) < 2:
        return ordered
    angles = [angle_from(depot, s.lat, s.lng) for s in ordered]
    gaps = [(angles[(i + 1) % len(angles)] - angles[i]) % (2 * math.pi) for i in range(len(angles))]
    start = (gaps.index(max(gaps)) + 1) % len(ordered)
    return ordered[start:] + ordered[:start]


def load_of(group):
    return sum(len(riders) for _, riders in group)


def sweep_groups(items, depot, *, route_count, max_load, max_stops):
    """
    Agrupa os itens em fatias vizinhas. route_count é a meta (equilibra a lotação); max_load e
    max_stops são limites: se forem atingidos, abre-se uma rota além da meta.
    """
    by_stop = {id(stop): (stop, riders) for stop, riders in items}
    swept = sweep_order([stop for stop, _ in items], depot)
    total = load_of(items)
    target = math.ceil(total / max(route_count, 1))

    groups, current, load = [], [], 0
    for stop in swept:
        riders = by_stop[id(stop)][1]
        full = load + len(riders) > max_load or len(current) >= max_stops
        reached_target = load >= target and len(groups) < route_count - 1
        if current and (full or reached_target):
            groups.append(current)
            current, load = [], 0
        current.append((stop, riders))
        load += len(riders)
    if current:
        groups.append(current)
    return groups


def absorb_small_groups(groups, *, min_load, max_load, max_stops):
    """
    Rota com menos de min_load passageiros some: cada parada dela vai para a rota mais próxima
    que ainda tenha lugar e paradas livres. Se alguma parada não couber, a rota fica.
    """
    result = [list(group) for group in groups]
    for small in sorted(result, key=load_of):
        if load_of(small) >= min_load:
            break
        others = [g for g in result if g is not small]
        placements = []
        for stop, riders in small:
            candidates = [
                g for g in others
                if load_of(g) + len(riders) + sum(len(r) for t, (_, r) in placements if t is g) <= max_load
                and len(g) + sum(1 for t, _ in placements if t is g) < max_stops
            ]
            if not candidates:
                placements = None
                break
            nearest = min(candidates, key=lambda g: min(_dist(stop, other) for other, _ in g))
            placements.append((nearest, (stop, riders)))
        if placements is None:
            continue
        for target, item in placements:
            target.append(item)
        result.remove(small)
    return result


def order_route(group, depot):
    """Ordem das paradas por inserção mais barata, terminando no depósito."""
    remaining = sorted(group, key=lambda item: _dist(item[0], depot), reverse=True)
    path = [remaining.pop(0)]
    for item in remaining:
        stop = item[0]
        best_index, best_cost = 0, _dist(stop, path[0][0])  # vira o novo início
        for index in range(1, len(path) + 1):
            previous = path[index - 1][0]
            following = path[index][0] if index < len(path) else depot
            cost = _dist(previous, stop) + _dist(stop, following) - _dist(previous, following)
            if cost < best_cost:
                best_index, best_cost = index, cost
        path.insert(best_index, item)
    return path


def mean_angle(group, depot):
    """Direção média da rota a partir do depósito, ponderada pelo número de passageiros."""
    x = sum(len(riders) * math.cos(angle_from(depot, stop.lat, stop.lng)) for stop, riders in group)
    y = sum(len(riders) * math.sin(angle_from(depot, stop.lat, stop.lng)) for stop, riders in group)
    return math.atan2(y, x)


def leg_minutes(meters):
    return meters * ROAD_FACTOR / 1000 / AVERAGE_SPEED_KMH * 60


def route_timeline(ordered_group, depot):
    """
    Estimativa (sem Routes API): minutos de cada parada até a chegada no depósito e a
    distância total em metros. Retorna ([minutos_antes_da_chegada por parada], metros).
    """
    points = [stop for stop, _ in ordered_group] + [depot]
    legs = [_dist(points[i], points[i + 1]) for i in range(len(points) - 1)]
    minutes_before_arrival = []
    for index in range(len(ordered_group)):
        remaining = sum(leg_minutes(m) for m in legs[index:]) + MINUTES_PER_STOP * (len(ordered_group) - index)
        minutes_before_arrival.append(math.ceil(remaining))
    return minutes_before_arrival, sum(legs)
