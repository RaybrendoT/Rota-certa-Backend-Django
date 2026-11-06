import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.eta import route_waypoints
from core.models import Route
from core.road_path import get_road_path


class Command(BaseCommand):
    help = (
        "Calcula (ou confere) o trajeto pelas ruas de cada rota ativa com a Google Routes API. "
        "Só chama a API para rotas cujas paradas mudaram, a menos que use --forcar."
    )

    def add_arguments(self, parser):
        parser.add_argument("--route-id", type=int, help="Só esta rota.")
        parser.add_argument("--forcar", action="store_true", help="Recalcula mesmo sem mudança nas paradas.")

    def handle(self, *args, **opts):
        key = settings.GOOGLE_MAPS_API_KEY
        if not key:
            raise CommandError("GOOGLE_MAPS_API_KEY vazia: coloque a chave no .env e rode de novo.")
        if not settings.ROAD_PATH_ENABLED:
            raise CommandError("ROAD_PATH_ENABLED=False no .env; o trajeto pelas ruas está desligado.")
        origin = "da variável de ambiente do sistema" if os.environ.get("GOOGLE_MAPS_API_KEY") else "do arquivo .env"
        self.stdout.write(f"Usando a chave terminada em ...{key[-4:]} ({len(key)} caracteres, lida {origin}).")

        routes = Route.objects.filter(is_active=True).order_by("id")
        if opts["route_id"]:
            routes = routes.filter(pk=opts["route_id"])
        ok = 0
        for route in routes:
            waypoints = route_waypoints(route)
            road = get_road_path(route, [(p.lat, p.lng) for p in waypoints], force=opts["forcar"])
            if road is None:
                self.stdout.write(self.style.WARNING(
                    f"#{route.id} {route.name}: linha reta (a API falhou; veja o aviso acima ou use --forcar)"
                ))
                continue
            ok += 1
            km = (route.road_path or {}).get("distance_meters") or 0
            self.stdout.write(f"#{route.id} {route.name}: pelas ruas, {len(road['points'])} pontos, {km / 1000:.1f} km")
        self.stdout.write(self.style.SUCCESS(f"{ok} de {routes.count()} rotas com trajeto pelas ruas."))
