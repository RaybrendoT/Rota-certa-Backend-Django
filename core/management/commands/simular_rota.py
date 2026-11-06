import math
import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.eta import cumulative_distances, route_path_with_source
from core.models import AccessAccount, Route, RouteEvent, RouteStop
from core.notifications import record_vehicle_location


def bearing_deg(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class Command(BaseCommand):
    help = (
        "Faz o ônibus de uma rota 'andar' pelas paradas até a empresa, gravando a posição a cada "
        "poucos segundos, como o app do motorista. Serve para testar o mapa ao vivo, a previsão de "
        "chegada e os avisos sem um motorista na rua. Ctrl+C para parar."
    )

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--route-id", type=int, help="ID da rota.")
        target.add_argument("--email", help="E-mail de um colaborador: simula a rota dele.")
        parser.add_argument("--minutos", type=float, default=5.0,
                            help="Duração aproximada do trajeto inteiro (padrão: 5 min, bom para demonstração).")
        parser.add_argument("--speed-kmh", type=float, default=None,
                            help="Velocidade fixa em km/h (ignora --minutos). Ex.: 30 = ritmo real.")
        parser.add_argument("--interval", type=float, default=3.0, help="Segundos entre posições (padrão: 3).")
        parser.add_argument("--stop-seconds", type=float, default=10.0, help="Tempo parado em cada parada (padrão: 10).")
        parser.add_argument("--no-events", action="store_true", help="Não registrar os eventos de saída e chegada.")

    def handle(self, *args, **opts):
        route = self.resolve_route(opts)
        path, source = route_path_with_source(route)
        if len(path) < 2:
            raise CommandError("A rota precisa de pelo menos 2 pontos (paradas/empresa) com coordenadas.")
        cum = cumulative_distances(path)
        if opts["speed_kmh"]:
            speed = max(opts["speed_kmh"], 1) / 3.6
        else:
            stops_time = opts["stop_seconds"] * sum(1 for p in path if p.stop_id is not None)
            speed = cum[-1] / max(opts["minutos"] * 60 - stops_time, 30)
        step = speed * opts["interval"]
        label = "pelas ruas" if source == "google" else "em linha reta (sem trajeto do Google)"
        self.stdout.write(self.style.SUCCESS(
            f"Simulando '{route.name}' {label}: {cum[-1] / 1000:.1f} km a {speed * 3.6:.0f} km/h. Ctrl+C para parar."
        ))
        try:
            self.run(route, path, cum, step, opts)
        except KeyboardInterrupt:
            self.stdout.write("\nSimulação interrompida.")

    def resolve_route(self, opts):
        if opts["route_id"]:
            route = Route.objects.filter(pk=opts["route_id"]).first()
            if route is None:
                raise CommandError(f"Rota {opts['route_id']} não encontrada.")
            return route
        account = AccessAccount.objects.filter(email=opts["email"].strip().lower()).select_related("person").first()
        if account is None:
            raise CommandError(f"Conta {opts['email']} não encontrada.")
        rp = account.person.route_passengers.filter(is_active=True, route__is_active=True).first()
        if rp is None:
            raise CommandError(f"{opts['email']} não é passageiro de nenhuma rota.")
        return rp.route

    def run(self, route, path, cum, step, opts):
        if not opts["no_events"]:
            self.event(route, "departure", "Simulação: saída da garagem", path[0])
        stop_names = dict(RouteStop.objects.filter(route=route).values_list("id", "meeting_point__name"))
        # Pontos onde o ônibus "chega": paradas e o fim do trajeto (os demais são curvas das ruas).
        checkpoints = [j for j, p in enumerate(path) if p.stop_id is not None or j == len(path) - 1]
        along = 0.0
        while True:
            lat, lng, seg = self.point_at(path, cum, along)
            record_vehicle_location(route, {
                "latitude": lat, "longitude": lng, "speed": step / opts["interval"],
                "heading": bearing_deg((path[seg].lat, path[seg].lng), (path[seg + 1].lat, path[seg + 1].lng)),
            })
            while checkpoints and along >= cum[checkpoints[0]]:
                point = path[checkpoints.pop(0)]
                name = stop_names.get(point.stop_id, "empresa")
                self.stdout.write(f"  [{timezone.localtime():%H:%M:%S}] chegou em {name}")
                if point.stop_id is not None and opts["stop_seconds"] > 0:
                    time.sleep(opts["stop_seconds"])
            if along >= cum[-1]:
                break
            time.sleep(opts["interval"])
            along = min(along + step, cum[-1])
        if not opts["no_events"]:
            self.event(route, "arrival", "Simulação: chegada na empresa", path[-1])
        self.stdout.write(self.style.SUCCESS("Fim do trajeto."))

    @staticmethod
    def point_at(path, cum, along):
        for i in range(len(path) - 1):
            if along <= cum[i + 1] or i == len(path) - 2:
                length = cum[i + 1] - cum[i]
                t = 0.0 if length == 0 else max(0.0, min(1.0, (along - cum[i]) / length))
                a, b = path[i], path[i + 1]
                return a.lat + (b.lat - a.lat) * t, a.lng + (b.lng - a.lng) * t, i
        return path[-1].lat, path[-1].lng, len(path) - 2

    @staticmethod
    def event(route, event_type, description, point):
        RouteEvent.objects.create(
            route=route, event_type=event_type, description=description,
            latitude=round(point.lat, 6), longitude=round(point.lng, 6),
        )
