import re


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
