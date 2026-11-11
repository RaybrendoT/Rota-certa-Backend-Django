"""
Cria as rotas fixas de um turno passando por todos os colaboradores da empresa: cada um embarca
no ponto de encontro mais perto de casa, e os pontos são agrupados por região até a lotação de
cada ônibus (ver core/fixed_routes.py).

Exemplos:
    # só mostra o que seria criado
    python manage.py criar_rotas_fixas --empresa MostraTech --chegada 07:00 --dry-run
    # cria (usa o turno das 07:00 já cadastrado; se não existir, cria Seg a Sex)
    python manage.py criar_rotas_fixas --empresa MostraTech --chegada 07:00
    # refaz do zero as rotas fixas desse turno e avisa passageiros e motoristas no app
    python manage.py criar_rotas_fixas --empresa MostraTech --chegada 07:00 --recriar --avisar

Quem já está numa rota fixa do turno é mantido onde está (rodar de novo só inclui quem ficou de
fora). A empresa precisa de pontos de encontro (add_manaus_meeting_points) e do endereço da sede.
"""
from datetime import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import fixed_routes
from core.models import Company, Schedule
from core.route_builder import BuilderError

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


class Command(BaseCommand):
    help = "Cria as rotas fixas de um turno cobrindo todos os colaboradores da empresa."

    def add_arguments(self, parser):
        parser.add_argument("--empresa", required=True, help='Nome da empresa, ex.: "MostraTech".')
        parser.add_argument("--chegada", required=True, help="Horário de chegada na empresa (HH:MM).")
        parser.add_argument(
            "--dias", default="MON,TUE,WED,THU,FRI",
            help="Dias do turno, se ele ainda não existir (padrão: MON,TUE,WED,THU,FRI).",
        )
        parser.add_argument("--max-paradas", type=int, default=fixed_routes.MAX_STOPS, help="Paradas por rota (padrão 12).")
        parser.add_argument(
            "--folga", type=int, default=fixed_routes.SLACK_PERCENT,
            help="Porcentagem de lugares deixada livre em cada ônibus (padrão 10).",
        )
        parser.add_argument("--incluir-gestao", action="store_true", help="Inclui gestores e gerentes como passageiros.")
        parser.add_argument("--recriar", action="store_true", help="Apaga antes as rotas fixas deste turno.")
        parser.add_argument("--avisar", action="store_true", help="Avisa passageiros e motoristas no app.")
        parser.add_argument("--dry-run", action="store_true", help="Só mostra o que seria criado.")

    def handle(self, *args, **options):
        company = Company.objects.filter(name=options["empresa"]).first()
        if company is None:
            raise CommandError(f"Empresa não encontrada: {options['empresa']}")
        try:
            arrival = time.fromisoformat(options["chegada"])
        except ValueError:
            raise CommandError("Use --chegada no formato HH:MM.") from None
        days = [d.strip().upper() for d in options["dias"].split(",") if d.strip()]
        if not days or any(d not in WEEKDAYS for d in days):
            raise CommandError(f"--dias aceita {', '.join(WEEKDAYS)} separados por vírgula.")
        if not 0 <= options["folga"] < 100:
            raise CommandError("--folga deve ficar entre 0 e 99.")

        with transaction.atomic():
            schedule = Schedule.objects.filter(company=company, time=arrival, is_active=True).order_by("id").first()
            if schedule is None:
                schedule = Schedule.objects.create(company=company, time=arrival, weekdays=days)
                self.stdout.write(f"Turno criado: {schedule}")
            if options["recriar"]:
                removed = fixed_routes.remove_existing(company, schedule)
                self.stdout.write(f"Rotas fixas apagadas do turno: {removed}")
            try:
                planned = fixed_routes.plan(
                    company, schedule, max_stops=options["max_paradas"], slack_percent=options["folga"],
                    include_managers=options["incluir_gestao"],
                )
            except (fixed_routes.FixedRoutesError, BuilderError) as exc:
                raise CommandError(str(exc)) from exc
            self.report(planned)
            if options["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Simulação (--dry-run): nada foi gravado."))
                return
            if not planned.routes:
                self.stdout.write("Nada a criar: todos já estão em rotas fixas deste turno (ou não há colaboradores).")
                return
            created = fixed_routes.apply(planned, notify_people=options["avisar"])
        self.stdout.write(self.style.SUCCESS(
            f"{len(created)} rotas fixas criadas em {company.name} para {planned.passengers} colaboradores"
            + (" (passageiros e motoristas avisados)." if options["avisar"] else ".")
        ))

    def report(self, planned):
        schedule = planned.schedule
        self.stdout.write(f"\n{planned.company.name} · turno {schedule}")
        for route in planned.routes:
            vehicle = route.vehicle
            self.stdout.write(
                f"  R{route.number:<2} {vehicle.plate_number} ({route.load}/{vehicle.capacity}) "
                f"{len(route.stops):>2} paradas · {route.meters / 1000:5.1f} km · ~{route.ride_minutes:>3} min · "
                f"{vehicle.driver.name} · {route.name_suffix}"
            )
        if planned.routes:
            longest = max(route.ride_minutes for route in planned.routes)
            seats = sum(route.vehicle.capacity for route in planned.routes)
            self.stdout.write(
                f"  Total: {len(planned.routes)} rotas, {planned.passengers} colaboradores em {seats} lugares; "
                f"viagem mais longa ~{longest} min."
            )
        if planned.already_riding:
            self.stdout.write(f"  Já estão em rota fixa do turno (mantidos): {len(planned.already_riding)}")
        if planned.without_home:
            names = ", ".join(p.name for p in planned.without_home[:10])
            more = f" e mais {len(planned.without_home) - 10}" if len(planned.without_home) > 10 else ""
            self.stdout.write(self.style.WARNING(f"  Sem endereço (ficaram de fora): {names}{more}"))
        if planned.far:
            worst = max(meters for _, _, meters in planned.far)
            self.stdout.write(self.style.WARNING(
                f"  {len(planned.far)} embarcam a mais de 1 km de casa (o mais longe a {worst / 1000:.1f} km)."
            ))
        if planned.busy_drivers:
            self.stdout.write(f"  Motoristas ocupados perto do horário (fora): {len(planned.busy_drivers)}")
