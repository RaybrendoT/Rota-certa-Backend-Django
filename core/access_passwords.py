import contextvars
import re
from contextlib import contextmanager

from django.contrib.auth.hashers import PBKDF2PasswordHasher, get_hashers_by_algorithm

# Importação em lote (centenas de contas): a senha inicial continua com PBKDF2-SHA256 e sal, só
# que com menos iterações, para 500 contas caberem numa requisição. Ela vale até o 1º acesso,
# quando a troca é obrigatória (must_change_password) e a senha nova usa o hasher padrão.
FAST_INITIAL_ITERATIONS = 20_000
_fast_initial = contextvars.ContextVar("fast_initial_passwords", default=False)


@contextmanager
def fast_initial_passwords():
    token = _fast_initial.set(True)
    try:
        yield
    finally:
        _fast_initial.reset(token)


def set_initial_password(account, raw_password):
    """
    Grava a senha inicial na conta (sem salvar): rápida dentro de fast_initial_passwords().
    Só usa o modo rápido se o PBKDF2 estiver em PASSWORD_HASHERS (senão o login não conferiria).
    """
    if _fast_initial.get() and PBKDF2PasswordHasher.algorithm in get_hashers_by_algorithm():
        hasher = PBKDF2PasswordHasher()
        account.password_hash = hasher.encode(raw_password, hasher.salt(), iterations=FAST_INITIAL_ITERATIONS)
    else:
        account.set_password(raw_password)


def generate_initial_access_password(person) -> str:
    """
    Gera a senha inicial no formato matrícula + últimos 4 dígitos do telefone.

    Para EMPLOYEE, usa employee_code.
    Para DRIVER, usa license_number.

    Ex.: employee_code=101010 e phone=92992281234 -> 1010101234.
    """
    registration = person.employee_code if person.type == "EMPLOYEE" else person.license_number
    phone_digits = re.sub(r"\D", "", person.phone or "")
    return f"{registration}{phone_digits[-4:]}"
