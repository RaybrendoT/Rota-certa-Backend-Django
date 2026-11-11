"""
Detecta os atrasos do dia (rota não iniciada, passou do horário, chegou atrasada) em todas as
empresas e resolve os que já se resolveram. Ver core/occurrences.py.

O painel já roda a detecção ao abrir o dashboard ou as ocorrências; agende este comando para que
os atrasos fiquem registrados mesmo sem ninguém com o painel aberto. Na EC2 (crontab -e):

    */5 * * * * cd ~/rotacerta/rota-certa-backend-django/deploy && docker compose -p rotacerta exec -T backend python manage.py detectar_ocorrencias

Uso: python manage.py detectar_ocorrencias [--company "MostraTech"]
"""
from django.core.management.base import BaseCommand, CommandError

from core import occurrences
from core.models import Company


class Command(BaseCommand):
    help = "Registra os atrasos detectados pelo horário das rotas do dia (ocorrências do sistema)."

    def add_arguments(self, parser):
        parser.add_argument("--company", action="append", help="Nome da empresa (pode repetir). Padrão: todas.")

    def handle(self, *args, **options):
        companies = Company.objects.filter(is_active=True).order_by("id")
        if options["company"]:
            companies = companies.filter(name__in=options["company"])
            missing = set(options["company"]) - set(companies.values_list("name", flat=True))
            if missing:
                raise CommandError(f"Empresa não encontrada: {', '.join(sorted(missing))}")
        total = 0
        for company in companies:
            created = occurrences.detect(company, force=True)
            total += created
            if created:
                self.stdout.write(f"{company.name}: {created} ocorrência(s) nova(s)")
        self.stdout.write(f"Total: {total} ocorrência(s) nova(s).")
