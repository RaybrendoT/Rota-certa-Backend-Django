from django.core.management.base import BaseCommand

from core.access_passwords import generate_initial_access_password
from core.models import AccessAccount


class Command(BaseCommand):
    help = (
        "Redefine senhas iniciais usando a regra atual: "
        "matrícula + últimos 4 dígitos do telefone."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Mostra quais contas seriam alteradas, sem salvar.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help=(
                "Redefine também contas com must_change_password=False. "
                "Por padrão, altera apenas contas com must_change_password=True."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        include_all = options["all"]

        qs = AccessAccount.objects.select_related("person").filter(
            is_active=True,
            person__is_active=True,
        )

        if not include_all:
            qs = qs.filter(must_change_password=True)

        total = 0

        for account in qs.order_by("id"):
            person = account.person
            new_password = generate_initial_access_password(person)

            self.stdout.write(
                f"Conta id={account.id} | pessoa={person.name} | email={account.email}"
            )

            if dry_run:
                total += 1
                continue

            account.set_password(new_password)
            account.save(update_fields=["password_hash", "updated_at"])
            total += 1

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f"Simulação concluída: {total} conta(s) seriam alteradas.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f"Pronto: {total} senha(s) redefinida(s).")
            )
