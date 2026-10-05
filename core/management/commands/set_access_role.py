from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.models import AccessAccount, normalize_email


class Command(BaseCommand):
    help = (
        "Define o perfil de acesso (EMPLOYEE, MANAGER ou GERENTE) da conta com o e-mail informado. "
        "É a forma de criar o primeiro gestor/gerente: cadastre a pessoa (tipo EMPLOYEE) e promova-a "
        "aqui ou pelo Django admin. Exemplo: manage.py set_access_role 1010@empresa.com MANAGER"
    )

    def add_arguments(self, parser):
        parser.add_argument("email", help="E-mail de login da conta (AccessAccount.email).")
        parser.add_argument(
            "role",
            choices=[
                AccessAccount.Role.EMPLOYEE,
                AccessAccount.Role.MANAGER,
                AccessAccount.Role.GERENTE,
            ],
            help="Novo perfil. DRIVER não é atribuível: vem do tipo da pessoa.",
        )

    def handle(self, *args, **options):
        email = normalize_email(options["email"])
        try:
            account = AccessAccount.objects.select_related("person").get(email=email)
        except AccessAccount.DoesNotExist:
            raise CommandError(f"Nenhuma conta com o e-mail {email!r}.")

        previous = account.role
        account.role = options["role"]
        try:
            account.save(update_fields=["role", "updated_at"])
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages))

        self.stdout.write(
            self.style.SUCCESS(f"{account.email}: perfil {previous} -> {account.role}")
        )
