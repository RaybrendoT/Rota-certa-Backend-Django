import csv
import io
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from core.dates import local_today
from core.models import EtaSample, StopArrival

# Momentos antes da chegada usados no resumo (minutos) e a tolerância de cada janela.
CHECKPOINTS = (10, 5, 2)
WINDOW_MINUTES = 1.0


class Command(BaseCommand):
    help = (
        "Teste em campo: compara a previsão de chegada (ETA) registrada durante a rota com a hora em "
        "que o ônibus realmente chegou ao ponto de cada passageiro. Gera um CSV e um resumo do erro."
    )

    def add_arguments(self, parser):
        parser.add_argument("--data", help="Dia da viagem (AAAA-MM-DD). Padrão: hoje.")
        parser.add_argument("--rota", type=int, help="Só esta rota (ID).")
        parser.add_argument("--saida", help="Arquivo CSV. Padrão: imprime o CSV na tela.")

    def handle(self, *args, **opts):
        try:
            day = date.fromisoformat(opts["data"]) if opts["data"] else local_today()
        except ValueError:
            raise CommandError("Use --data no formato AAAA-MM-DD.")
        arrivals = StopArrival.objects.filter(service_date=day).select_related(
            "passenger__person", "passenger__route", "passenger__route_stop__meeting_point"
        )
        if opts["rota"]:
            arrivals = arrivals.filter(passenger__route_id=opts["rota"])

        rows = []
        for arrival in arrivals:
            p = arrival.passenger
            samples = EtaSample.objects.filter(
                passenger=p, service_date=day, recorded_at__lte=arrival.arrived_at
            )
            for s in samples:
                real = (arrival.arrived_at - s.recorded_at).total_seconds() / 60
                predicted = s.eta_seconds / 60
                rows.append({
                    "data": day.isoformat(),
                    "rota": p.route.name,
                    "passageiro": p.person.name,
                    "ponto": p.route_stop.meeting_point.name if p.route_stop_id else "",
                    "registrado_em": s.recorded_at.isoformat(timespec="seconds"),
                    "previsto_min": round(predicted, 1),
                    "real_min": round(real, 1),
                    "erro_min": round(predicted - real, 1),
                    "distancia_m": s.distance_meters,
                    "velocidade_kmh": round(s.speed * 3.6) if s.speed is not None else "",
                })
        if not rows:
            raise CommandError(f"Nenhuma chegada registrada em {day:%d/%m/%Y}. Faça a rota com o GPS ligado.")

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        if opts["saida"]:
            with open(opts["saida"], "w", newline="", encoding="utf-8") as fh:
                fh.write(buffer.getvalue())
        else:
            self.stdout.write(buffer.getvalue(), ending="")

        log = self.stderr if not opts["saida"] else self.stdout
        errors = [abs(r["erro_min"]) for r in rows]
        log.write(f"\n{arrivals.count()} chegadas, {len(rows)} previsões em {day:%d/%m/%Y}.")
        log.write(f"Erro absoluto médio: {sum(errors) / len(errors):.1f} min "
                  f"(média com sinal: {sum(r['erro_min'] for r in rows) / len(rows):+.1f} min; "
                  "positivo = previu mais tempo do que levou).")
        for minutes in CHECKPOINTS:
            window = [r for r in rows if abs(r["real_min"] - minutes) <= WINDOW_MINUTES]
            if window:
                mae = sum(abs(r["erro_min"]) for r in window) / len(window)
                log.write(f"  ~{minutes} min antes da chegada: erro médio {mae:.1f} min ({len(window)} previsões)")
        if opts["saida"]:
            log.write(self.style.SUCCESS(f"CSV salvo em {opts['saida']}"))
