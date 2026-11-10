"""
Ingestão de pessoas de uma empresa a partir de um CSV (comando importar_csv).

Uma linha por pessoa, com o perfil na coluna "perfil":
  EMPRESA      endereço da sede (destino das rotas); no máximo uma linha
  COLABORADOR  funcionário que usa o transporte (matrícula + endereço de casa)
  GESTOR       gestor de transporte da empresa (matrícula; entra no painel)
  GERENTE      gerente (matrícula; entra no painel com a visão de clientes)
  MOTORISTA    motorista (CNH) e o veículo dele (placa, modelo, capacidade)

O e-mail de acesso é sempre {matrícula ou CNH}@{domínio da empresa}, como no resto do sistema;
a coluna "email", se preenchida, precisa bater com ele. A senha inicial segue a regra de
core.access_passwords (matrícula/CNH + 4 últimos dígitos do telefone), com troca no 1º login.

Tudo é validado antes de gravar: com qualquer erro nada é importado. Rodar de novo é seguro:
quem já existe (mesma matrícula/CNH na empresa) é pulado.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import AccessAccount, Address, Company, Person, Vehicle, normalize_email

COLUMNS = [
    "perfil", "nome", "matricula", "cnh", "telefone", "email",
    "logradouro", "numero", "bairro", "cidade", "uf", "cep", "latitude", "longitude",
    "placa", "modelo_veiculo", "capacidade",
]
PROFILES = ("EMPRESA", "COLABORADOR", "GESTOR", "GERENTE", "MOTORISTA")
ROLE_BY_PROFILE = {"GESTOR": AccessAccount.Role.MANAGER, "GERENTE": AccessAccount.Role.GERENTE}
ADDRESS_FIELDS = ("logradouro", "numero", "cidade", "uf", "cep", "latitude", "longitude")
# Latitude/longitude plausíveis para o Brasil (pega coordenada trocada ou sem sinal).
LAT_RANGE, LNG_RANGE = (-34.0, 6.0), (-74.0, -34.0)


class ImportErrors(Exception):
    def __init__(self, errors):
        super().__init__(f"{len(errors)} erro(s) no CSV")
        self.errors = errors


@dataclass
class ImportResult:
    company: Company | None = None
    created: dict = field(default_factory=lambda: {p: 0 for p in PROFILES if p != "EMPRESA"})
    vehicles: int = 0
    skipped: int = 0
    company_address: bool = False


def read_rows(data: bytes):
    """Lê o CSV (UTF-8 ou Windows-1252; vírgula ou ponto e vírgula). Devolve [(nº da linha, dict)]."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    first_line = text.splitlines()[0] if text.strip() else ""
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    header = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in ("perfil", "nome") if c not in header]
    if missing:
        raise ImportErrors([f"Cabeçalho sem as colunas obrigatórias: {', '.join(missing)}."])
    rows = []
    for number, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        if any(row.values()):
            rows.append((number, row))
    return rows


def _decimal(value):
    try:
        return Decimal(value.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


def _validate(rows, company_name, domain, existing_codes, existing_plates):
    errors, people, company_row = [], [], None
    seen_codes, seen_plates, seen_emails = set(), set(), set()
    for number, row in rows:
        def err(message, number=number):
            errors.append(f"Linha {number}: {message}")

        profile = row.get("perfil", "").upper()
        if profile not in PROFILES:
            err(f"perfil '{row.get('perfil', '')}' inválido (use {', '.join(PROFILES)}).")
            continue
        if not row.get("nome"):
            err("nome vazio.")

        if profile == "EMPRESA":
            if company_row is not None:
                err("só pode haver uma linha EMPRESA.")
            company_row = (number, row)
            if row.get("nome") and row["nome"].casefold() != company_name.casefold():
                err(f"a linha EMPRESA é de '{row['nome']}', mas o comando é para '{company_name}'.")
        else:
            is_driver = profile == "MOTORISTA"
            code_col = "cnh" if is_driver else "matricula"
            code = row.get(code_col, "")
            if not code:
                err(f"{code_col} vazia ({profile.lower()} precisa de {code_col}).")
            elif re.search(r"\s", code):
                err(f"{code_col} '{code}' não pode ter espaços (vira o e-mail de acesso).")
            elif code.lower() in seen_codes:
                err(f"{code_col} '{code}' repetida no arquivo.")
            seen_codes.add(code.lower())
            if not is_driver and row.get("cnh"):
                err("só motorista tem CNH.")
            if is_driver and row.get("matricula"):
                err("motorista não tem matrícula (use a coluna cnh).")
            digits = re.sub(r"\D", "", row.get("telefone", ""))
            if len(digits) < 10:
                err("telefone inválido (DDD + número).")
            if code:
                email = normalize_email(f"{code.lower()}@{domain}")
                if row.get("email") and normalize_email(row["email"]) != email:
                    err(f"e-mail '{row['email']}' não bate com o de acesso gerado ({email}).")
                if email in seen_emails:
                    err(f"e-mail {email} repetido no arquivo.")
                seen_emails.add(email)
            if is_driver:
                plate = re.sub(r"[\s-]", "", row.get("placa", "")).upper()
                if not plate:
                    err("motorista sem placa do veículo.")
                elif plate in seen_plates:
                    err(f"placa {plate} repetida no arquivo.")
                elif plate in existing_plates and code.lower() not in existing_codes:
                    err(f"placa {plate} já cadastrada em outro veículo.")
                seen_plates.add(plate)
                row["placa"] = plate
                if not row.get("modelo_veiculo"):
                    err("motorista sem modelo do veículo.")
                if not row.get("capacidade", "").isdigit() or int(row["capacidade"]) <= 0:
                    err("capacidade do veículo inválida.")
            people.append((number, profile, row))

        needs_address = profile in ("EMPRESA", "COLABORADOR")
        has_address = any(row.get(c) for c in ADDRESS_FIELDS)
        if needs_address or has_address:
            missing = [c for c in ADDRESS_FIELDS if not row.get(c)]
            if missing:
                err(f"endereço incompleto (falta {', '.join(missing)}).")
            else:
                lat, lng = _decimal(row["latitude"]), _decimal(row["longitude"])
                if lat is None or lng is None:
                    err("latitude/longitude não são números.")
                elif not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]):
                    err(f"coordenada fora do Brasil ({lat}, {lng}); confira se não estão trocadas.")
                else:
                    row["latitude"], row["longitude"] = lat.quantize(Decimal("0.000001")), lng.quantize(Decimal("0.000001"))
    if errors:
        raise ImportErrors(errors)
    return company_row, people


def _address(row, **owner):
    return Address(
        street=row["logradouro"][:100], number=row["numero"][:20], neighborhood=row.get("bairro", "")[:100],
        city=row["cidade"][:50], state=row["uf"][:50], zip_code=row["cep"][:10],
        latitude=row["latitude"], longitude=row["longitude"], source=Address.Source.PANEL, **owner,
    )


def import_people(data: bytes, company_name: str, domain: str = "", dry_run: bool = False) -> ImportResult:
    """Valida tudo e, sem erros (e sem dry_run), grava numa transação só."""
    rows = read_rows(data)
    company = Company.objects.filter(name__iexact=company_name).first()
    domain = (domain or (company.get_email_domain() if company else "")).strip().lower().lstrip("@")
    if not domain:
        raise ImportErrors(["Informe o domínio de e-mail da empresa (--dominio), ex.: mostratech.com.br."])
    if company and company.email_domain and company.get_email_domain() != domain:
        raise ImportErrors([f"A empresa {company.name} já usa o domínio {company.get_email_domain()}, não {domain}."])

    existing_codes = set()
    if company:
        existing_codes = {
            c.lower() for c in Person.objects.filter(company=company).values_list("employee_code", flat=True) if c
        } | {
            c.lower() for c in Person.objects.filter(company=company).values_list("license_number", flat=True) if c
        }
    existing_plates = set(Vehicle.objects.values_list("plate_number", flat=True))
    company_row, people = _validate(rows, company_name, domain, existing_codes, existing_plates)

    result = ImportResult(company=company)
    if dry_run:
        for _, profile, row in people:
            code = row.get("cnh") if profile == "MOTORISTA" else row.get("matricula")
            if code.lower() in existing_codes:
                result.skipped += 1
            else:
                result.created[profile] += 1
                result.vehicles += profile == "MOTORISTA"
        result.company_address = company_row is not None
        return result

    errors = []
    with transaction.atomic():
        if company is None:
            company = Company.objects.create(name=company_name, email_domain=domain)
        elif not company.email_domain:
            company.email_domain = domain
            company.save()
        result.company = company
        if company_row is not None:
            row = company_row[1]
            sede = Address.objects.filter(company=company, person__isnull=True, meeting_points__isnull=True, is_active=True)
            if not sede.filter(latitude=row["latitude"], longitude=row["longitude"], street=row["logradouro"]).exists():
                sede.update(is_active=False)  # a sede mudou: a nova passa a ser o destino das rotas
                _address(row, company=company).save()
                result.company_address = True

        for number, profile, row in people:
            is_driver = profile == "MOTORISTA"
            code = row["cnh"] if is_driver else row["matricula"]
            if code.lower() in existing_codes:
                result.skipped += 1
                continue
            lookup = {"license_number": code} if is_driver else {"employee_code": code}
            person = Person(
                company=company, name=row["nome"][:255], phone=row["telefone"][:30],
                type=Person.PersonType.DRIVER if is_driver else Person.PersonType.EMPLOYEE, **lookup,
            )
            person.save()  # o signal cria a conta de acesso com a senha inicial da regra
            account = AccessAccount.objects.filter(person=person).first()
            if account is None:
                errors.append(f"Linha {number}: conta de acesso não criada (e-mail já usado por outra pessoa?).")
                continue
            if profile in ROLE_BY_PROFILE:
                account.role = ROLE_BY_PROFILE[profile]
                account.save(update_fields=["role", "updated_at"])
            if row.get("latitude"):
                _address(row, person=person).save()
            if is_driver:
                Vehicle.objects.create(
                    company=company, driver=person, plate_number=row["placa"],
                    model=row["modelo_veiculo"][:100], capacity=int(row["capacidade"]),
                )
                result.vehicles += 1
            result.created[profile] += 1
        if errors:
            transaction.set_rollback(True)
            raise ImportErrors(errors)
    return result
