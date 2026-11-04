"""
Aplica a regra de embarque (core.home_pickup) aos passageiros que já estão nas rotas fixas.

Quem já embarca numa parada pública a até 500 m de casa continua nela. Os demais passam para a parada
da rota a até 500 m, para o ponto de encontro da empresa a até 500 m (que entra na rota) ou, em último
caso, para a porta de casa. Passageiros sem endereço ficam como estão.

--drop-empty-stops remove as paradas em que ninguém mais embarca; --notify avisa no app cada passageiro
que trocou de ponto e o motorista de cada rota que mudou.

Uso: python manage.py apply_pickup_rule [--company "Polo Digital Manaus"] [--drop-empty-stops] [--notify] [--dry-run]
"""
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from core.geo import distance_meters
from core.home_pickup import PICKUP_RADIUS_M, drop_empty_stops, drop_unused_home_stops, pickup_description, pickup_stop
from core.models import Notification, Route, RoutePassenger, RouteStop
from core.notifications import notify


class DryRun(Exception):
    """Desfaz a transação no fim da simulação."""


def notify_pickup_change(ride, previous_point_name):
    notify(
        [ride.person],
        kind=Notification.Kind.PICKUP_CHANGED,
        title="Novo ponto de embarque",
        message=(
            f"Na rota {ride.route.name}, seu embarque passou de {previous_point_name} para "
            f"{pickup_description(ride)}. O ponto foi escolhido pela proximidade do seu endereço."
        ),
        route=ride.route,
    )


def notify_route_updated(route):
    driver = route.vehicle.driver if route.vehicle else None
    if driver is None:
        return
    stops = route.stops.count()
    notify(
        [driver],
        kind=Notification.Kind.ROUTE_UPDATED,
        title="Rota atualizada",
        message=f"A rota {route.name} foi reorganizada pelos endereços dos passageiros: agora são {stops} paradas.",
        route=route,
    )


class Command(BaseCommand):
    help = "Aplica a regra de embarque a até 500 m (ou na porta de casa) aos passageiros das rotas fixas."

    def add_arguments(self, parser):
        parser.add_argument("--company", action="append", help="Nome da empresa (pode repetir). Padrão: todas.")
        parser.add_argument("--drop-empty-stops", action="store_true", help="Remove paradas sem nenhum passageiro.")
        parser.add_argument("--notify", action="store_true", help="Avisa passageiros que trocaram de ponto e motoristas.")
        parser.add_argument("--dry-run", action="store_true", help="Só mostra o que mudaria, sem gravar.")

    def handle(self, *args, **options):
        routes = Route.objects.filter(is_active=True, service_date__isnull=True).select_related("company", "vehicle__driver")
        if options["company"]:
            routes = routes.filter(company__name__in=options["company"])
        stops_before = RouteStop.objects.filter(route__in=routes).count()
        totals = Counter()
        try:
            with transaction.atomic():
                for route in routes.order_by("company_id", "name"):
                    counts, moved = self.apply(route)
                    if options["drop_empty_stops"]:
                        counts["dropped_stops"] = drop_empty_stops(route)
                    if options["notify"] and not options["dry_run"]:
                        for ride, previous in moved:
                            notify_pickup_change(ride, previous)
                        if moved or counts["dropped_stops"]:
                            notify_route_updated(route)
                            counts["routes_notified"] += 1
                    totals += counts
                stops_after = RouteStop.objects.filter(route__in=routes).count()
                if options["dry_run"]:
                    raise DryRun
        except DryRun:
            self.stdout.write("Simulação (nada foi gravado):")
        self.stdout.write(
            f"{totals['kept']} mantidos · {totals['route']} trocados para parada da rota · "
            f"{totals['new_point']} para ponto que entrou na rota · {totals['home']} na porta de casa · "
            f"{totals['no_address']} sem endereço · {totals['other_company']} de outra empresa (ignorados)"
        )
        self.stdout.write(
            f"Paradas nas rotas fixas: {stops_before} → {stops_after} ({totals['dropped_stops']} vazias removidas)"
        )
        if options["notify"] and not options["dry_run"]:
            moved_total = totals["route"] + totals["new_point"] + totals["home"]
            self.stdout.write(f"Avisos: {moved_total} passageiros e {totals['routes_notified']} motoristas")

    def apply(self, route):
        """(contagens, [(vínculo que mudou, nome do ponto anterior)])."""
        counts, moved = Counter(), []
        rides = RoutePassenger.objects.filter(route=route, is_active=True).select_related(
            "person__company", "route", "route_stop__meeting_point__address"
        )
        for ride in rides:
            if ride.person.company_id != route.company_id:
                counts["other_company"] += 1  # vínculo inconsistente: ponto e parada precisam ser da empresa da rota
                continue
            home = ride.person.addresses.filter(is_active=True).order_by("-created_at", "-id").first()
            if home is None:
                counts["no_address"] += 1
                continue
            current = ride.route_stop
            if (
                current is not None
                and current.meeting_point.home_of_id is None
                and distance_meters(home.latitude, home.longitude, current.meeting_point.address.latitude,
                                    current.meeting_point.address.longitude) <= PICKUP_RADIUS_M
            ):
                counts["kept"] += 1
                continue
            previous = current.meeting_point.name if current else "ponto não definido"
            stop, how = pickup_stop(route, ride.person, home)
            if stop.id == ride.route_stop_id:
                counts["kept"] += 1
                continue
            ride.route_stop = stop
            ride.save(update_fields=["route_stop", "updated_at"])
            counts[how] += 1
            moved.append((ride, previous))
        drop_unused_home_stops(route)
        return counts, moved
