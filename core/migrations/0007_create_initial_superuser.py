"""
Cria o superusuário inicial do Django admin (auth.User) a partir de variáveis de ambiente.

Numeração: esta migração nasceu como "0006" (ver `03-migrations-e-superuser.md`), mas o
repositório já tinha ganhado, nesse meio-tempo, `core/migrations/0006_access_account_role.py`
(adiciona `AccessAccount.role`, com o perfil MANAGER). Por isso ela entra como 0007,
dependendo de 0006_access_account_role — não altera nem depende do campo `role`.

Como funciona
-------------
* Lê DJANGO_SUPERUSER_USERNAME, DJANGO_SUPERUSER_PASSWORD e (opcional) DJANGO_SUPERUSER_EMAIL.
  São os mesmos nomes que o `manage.py createsuperuser --noinput` do Django usa.
* Se USERNAME ou PASSWORD não estiverem definidos, NÃO FAZ NADA (é o caso do desenvolvimento
  local e da suíte de testes). Nenhuma senha padrão existe no código.
* É idempotente: se já existir um usuário com esse username, não altera nada.
* A senha só passa por `make_password` (hash PBKDF2); nunca é gravada em log.
* Exige senha com pelo menos 12 caracteres, para falhar cedo em vez de subir um admin fraco.
* O reverse é vazio de propósito: desfazer a migração não apaga o superusuário.

Importante
----------
* Este superusuário acessa apenas o Django admin (/admin/). NÃO é uma conta de login da API:
  a API usa core.AccessAccount (ligada a uma Person), que agora tem seu próprio perfil MANAGER
  (`core/migrations/0006_access_account_role.py`, `core/permissions.py`) para administrar
  pela API/frontend — são coisas diferentes (ver `03-migrations-e-superuser.md`, seção 2).
* Só roda quando a migração é aplicada. Trocar a senha depois exige `changepassword`
  (ou o admin), não reaplicar a migração.
"""
import logging
import os

from django.contrib.auth.hashers import make_password
from django.db import migrations

logger = logging.getLogger(__name__)

ENV_USERNAME = "DJANGO_SUPERUSER_USERNAME"
ENV_PASSWORD = "DJANGO_SUPERUSER_PASSWORD"
ENV_EMAIL = "DJANGO_SUPERUSER_EMAIL"

MIN_PASSWORD_LENGTH = 12


def create_initial_superuser(apps, schema_editor):
    username = (os.environ.get(ENV_USERNAME) or "").strip()
    password = os.environ.get(ENV_PASSWORD) or ""
    email = (os.environ.get(ENV_EMAIL) or "").strip()

    if not username or not password:
        logger.info("Superusuário inicial não criado: %s/%s não definidos.", ENV_USERNAME, ENV_PASSWORD)
        return

    if len(password) < MIN_PASSWORD_LENGTH:
        raise RuntimeError(
            f"{ENV_PASSWORD} deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."
        )

    User = apps.get_model("auth", "User")
    manager = User.objects.using(schema_editor.connection.alias)

    if manager.filter(username=username).exists():
        logger.info("Superusuário '%s' já existe; nada a fazer.", username)
        return

    manager.create(
        username=username,
        email=email,
        password=make_password(password),
        is_staff=True,
        is_superuser=True,
        is_active=True,
    )
    logger.info("Superusuário '%s' criado.", username)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_access_account_role"),
        # Garante que a tabela auth_user já está no formato final antes do INSERT.
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_initial_superuser, migrations.RunPython.noop),
    ]
