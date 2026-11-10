from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.importacao_csv import COLUMNS, ImportErrors, import_people


class Command(BaseCommand):
    help = (
        "Importa as pessoas de uma empresa a partir de um CSV: sede (EMPRESA), colaboradores, gestor, "
        "gerente e motoristas com veículo. Valida tudo antes; com qualquer erro, nada é gravado. "
        f"Colunas: {', '.join(COLUMNS)}."
    )

    def add_arguments(self, parser):
        parser.add_argument("arquivo", help="Caminho do CSV (UTF-8 ou Excel/Windows; vírgula ou ponto e vírgula).")
        parser.add_argument("--empresa", required=True, help='Nome da empresa, ex.: "MostraTech". Criada se não existir.')
        parser.add_argument("--dominio", default="", help="Domínio dos e-mails de acesso, ex.: mostratech.com.br.")
        parser.add_argument("--dry-run", action="store_true", help="Só valida e mostra o que seria criado.")

    def handle(self, *args, **opts):
        path = Path(opts["arquivo"])
        if not path.is_file():
            raise CommandError(f"Arquivo não encontrado: {path}")
        try:
            result = import_people(path.read_bytes(), opts["empresa"], opts["dominio"], dry_run=opts["dry_run"])
        except ImportErrors as exc:
            for message in exc.errors[:50]:
                self.stderr.write(self.style.ERROR(message))
            if len(exc.errors) > 50:
                self.stderr.write(f"... e mais {len(exc.errors) - 50} erro(s).")
            raise CommandError("Nada foi importado. Corrija o CSV e rode de novo.")

        c = result.created
        prefix = "[dry-run] seria criado" if opts["dry_run"] else "Importado"
        self.stdout.write(self.style.SUCCESS(
            f"{prefix} em {opts['empresa']}: {c['COLABORADOR']} colaboradores, {c['GESTOR']} gestor(es), "
            f"{c['GERENTE']} gerente(s), {c['MOTORISTA']} motoristas, {result.vehicles} veículos"
            f"{', endereço da sede' if result.company_address else ''}; {result.skipped} já existiam."
        ))
        if not opts["dry_run"]:
            self.stdout.write(
                "Login: e-mail = matrícula (ou CNH)@domínio; senha inicial = matrícula (ou CNH) + "
                "4 últimos dígitos do telefone, com troca obrigatória no 1º acesso."
            )
