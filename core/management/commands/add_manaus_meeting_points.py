"""
Adiciona às empresas os pontos de encontro reais de Manaus (endereços geocodificados em
core/seed_data/manaus_bairros.py), sem duplicar: um ponto só entra se a empresa não tiver outro a
menos de --min-gap metros. Com --cover-employees, cria também pontos onde há colaboradores a mais de
500 m de qualquer ponto (sugestões H3, com endereço de rua real pela Geocoding API). Pode rodar de novo à vontade.

Uso: python manage.py add_manaus_meeting_points [--company "Polo Digital Manaus"] [--min-gap 150] [--cover-employees]
"""
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import pickup_points
from core.geo import distance_meters
from core.models import Address, Company, MeetingPoint, Person
from core.seed_data.manaus_bairros import BAIRROS

GENERIC_ZIP = "69000-000"
COVER_RADIUS_M = 500


class Command(BaseCommand):
    help = "Adiciona pontos de encontro reais de Manaus às empresas (sem duplicar pontos próximos)."

    def add_arguments(self, parser):
        parser.add_argument("--company", action="append", help="Nome da empresa (pode repetir). Padrão: todas.")
        parser.add_argument("--min-gap", type=int, default=150, help="Distância mínima até um ponto existente (m).")
        parser.add_argument(
            "--cover-employees", action="store_true",
            help="Cria pontos perto de quem continua a mais de 500 m de qualquer ponto (usa a Geocoding API).",
        )

    def handle(self, *args, **options):
        companies = Company.objects.order_by("id")
        if options["company"]:
            companies = companies.filter(name__in=options["company"])
            missing = set(options["company"]) - set(companies.values_list("name", flat=True))
            if missing:
                raise CommandError(f"Empresa não encontrada: {', '.join(sorted(missing))}")
        for company in companies:
            added = self.add_points(company, options["min_gap"])
            if options["cover_employees"]:
                added += self.cover_employees(company)
            self.stdout.write(f"{company.name}: +{added} pontos ({self.coverage(company)})")

    @transaction.atomic
    def add_points(self, company, min_gap):
        existing = [
            (float(mp.address.latitude), float(mp.address.longitude))
            for mp in MeetingPoint.objects.filter(company=company, home_of__isnull=True).select_related("address")
            if mp.address.latitude is not None
        ]
        added = 0
        for bairro in BAIRROS:
            for stop in bairro.stops:
                if any(distance_meters(stop.lat, stop.lng, lat, lng) < min_gap for lat, lng in existing):
                    continue
                address = Address.objects.create(
                    company=company, street=stop.street, number=stop.number, neighborhood=bairro.name,
                    city="Manaus", state="AM", zip_code=GENERIC_ZIP,
                    latitude=Decimal(f"{stop.lat:.6f}"), longitude=Decimal(f"{stop.lng:.6f}"),
                )
                MeetingPoint.objects.create(company=company, name=f"{bairro.name} – {stop.street}", address=address)
                existing.append((stop.lat, stop.lng))
                added += 1
        return added

    @transaction.atomic
    def cover_employees(self, company):
        """Um ponto por grupo de colaboradores descobertos (até 500 m a pé), no endereço de rua mais próximo."""
        result = pickup_points.suggest_pickup_points(
            company, walk_limit=COVER_RADIUS_M, min_people=1, max_suggestions=50
        )
        for suggestion in result["suggestions"]:
            found = suggestion["address"] or {}
            street = found.get("street") or "Rua sem nome"
            neighborhood = found.get("sublocality", "")
            address = Address.objects.create(
                company=company, street=street, number=found.get("number") or "S/N", neighborhood=neighborhood,
                city="Manaus", state="AM", zip_code=GENERIC_ZIP,
                latitude=Decimal(f"{suggestion['latitude']:.6f}"), longitude=Decimal(f"{suggestion['longitude']:.6f}"),
            )
            name = f"{neighborhood} – {street}" if neighborhood else street
            MeetingPoint.objects.create(company=company, name=name, address=address)
        return len(result["suggestions"])

    def coverage(self, company):
        """Quantos colaboradores com endereço moram a até 500 m de algum ponto."""
        points = [
            (float(mp.address.latitude), float(mp.address.longitude))
            for mp in MeetingPoint.objects.filter(company=company, is_active=True, home_of__isnull=True).select_related("address")
        ]
        homes = [
            (float(a.latitude), float(a.longitude))
            for person in Person.objects.filter(company=company, type=Person.PersonType.EMPLOYEE, is_active=True)
            .prefetch_related("addresses")
            for a in person.addresses.all() if a.is_active
        ]
        near = sum(1 for lat, lng in homes if any(distance_meters(lat, lng, plat, plng) <= 500 for plat, plng in points))
        return f"{len(points)} pontos; {near} de {len(homes)} colaboradores com endereço a até 500 m de um ponto"
