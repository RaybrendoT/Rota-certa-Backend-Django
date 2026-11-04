"""
Lista de colaboradores anexada ao planejamento (CSV ou Excel .xlsx com matrícula e nome).

O arquivo é lido e conferido com o cadastro da empresa; o painel mostra o resultado e envia ao
assistente só as matrículas encontradas. Cabeçalho é opcional: sem ele, a coluna com mais dígitos é
a matrícula e a outra é o nome.
"""
import csv
import io
import zipfile

from openpyxl import load_workbook

from core.planning.planner import EmployeeDirectory, active_employees, normalize

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000
CODE_PREFIXES = ("matr", "cod", "registro", "cracha", "chapa")
CODE_WORDS = {"re", "mat", "id"}
NAME_PREFIXES = ("nome", "colaborador", "funcionario", "empregado")


class EmployeeListError(Exception):
    """Arquivo que não dá para ler; a mensagem vai para o gestor."""


def read_table(name: str, data: bytes):
    """Linhas do arquivo como listas de textos (sem linhas vazias)."""
    if len(data) > MAX_BYTES:
        raise EmployeeListError("Arquivo maior que 2 MB.")
    extension = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if extension == "xls" or data.startswith(b"\xd0\xcf\x11\xe0"):
        raise EmployeeListError("Formato .xls antigo não é suportado: salve como .xlsx ou .csv.")
    if extension == "xlsx" or data.startswith(b"PK"):
        rows = _xlsx_rows(data)
    elif extension in ("csv", "txt", ""):
        rows = _csv_rows(data)
    else:
        raise EmployeeListError("Envie um arquivo .csv ou .xlsx.")
    rows = [row for row in rows if any(row)]
    if not rows:
        raise EmployeeListError("O arquivo está vazio.")
    return rows


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # matrícula numérica no Excel chega como 123.0
    return str(value).strip()


def _xlsx_rows(data: bytes):
    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise EmployeeListError("Não foi possível ler a planilha Excel.") from exc
    try:
        sheet = workbook.worksheets[0]
        rows = []
        for values in sheet.iter_rows(values_only=True):
            rows.append([_cell_text(v) for v in values])
            if len(rows) > MAX_ROWS + 1:
                break
        return rows
    finally:
        workbook.close()


def _csv_rows(data: bytes):
    # Excel em português salva CSV em cp1252; latin-1 nunca falha e fica como último recurso.
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    first_line = text.split("\n", 1)[0]
    delimiter = max(";,\t", key=first_line.count) if any(c in first_line for c in ";,\t") else ";"
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _header_kind(cell: str):
    words = normalize(cell).replace(".", " ").replace("_", " ").replace("/", " ").split()
    if any(w.startswith(CODE_PREFIXES) or w in CODE_WORDS for w in words):
        return "code"
    if any(w.startswith(NAME_PREFIXES) for w in words):
        return "name"
    return None


def find_columns(rows):
    """(índice da matrícula, índice do nome, tem cabeçalho). Índices podem ser None."""
    kinds = [_header_kind(cell) for cell in rows[0]]
    if "code" in kinds or "name" in kinds:
        code = kinds.index("code") if "code" in kinds else None
        name = kinds.index("name") if "name" in kinds else None
        return code, name, True

    width = max(len(row) for row in rows)
    if width == 1:
        return 0, None, False  # uma coluna só: cada valor é matrícula ou nome

    def digit_share(column):
        values = [row[column] for row in rows if column < len(row) and row[column]]
        return sum(any(c.isdigit() for c in v) for v in values) / max(len(values), 1)

    code, name = (0, 1) if digit_share(0) >= digit_share(1) else (1, 0)
    return code, name, False


def match_employee_list(company, file_name: str, data: bytes):
    rows = read_table(file_name, data)
    code_col, name_col, has_header = find_columns(rows)
    body = rows[1:] if has_header else rows
    if len(body) > MAX_ROWS:
        raise EmployeeListError(f"A lista tem mais de {MAX_ROWS} linhas.")

    directory = EmployeeDirectory(active_employees(company).select_related("access_account"))
    matched, ambiguous, not_found = [], [], []
    seen = set()
    duplicates = 0
    first_line = 2 if has_header else 1
    for offset, row in enumerate(body):
        code = row[code_col] if code_col is not None and code_col < len(row) else ""
        name = row[name_col] if name_col is not None and name_col < len(row) else ""
        if not code and not name:
            continue
        line = {"row": first_line + offset, "code": code, "name": name}

        person = directory.by_identifier(code) if code else None
        if person is not None:
            candidates = [person]
        else:
            # Coluna única sem dígitos, ou matrícula que não existe: tenta pelo nome.
            candidates = directory.by_name(name or code) if (name or not any(c.isdigit() for c in code)) else []

        if len(candidates) > 1:
            ambiguous.append({**line, "candidates": [_person_info(p) for p in candidates[:5]]})
            continue
        if not candidates:
            not_found.append(line)
            continue
        person = candidates[0]
        if person.id in seen:
            duplicates += 1
            continue
        seen.add(person.id)
        matched.append({
            **line,
            "person": _person_info(person),
            "matched_by": "code" if code and directory.by_identifier(code) else "name",
            "name_differs": bool(name) and not _same_person_name(name, person.name),
        })

    return {
        "file_name": file_name,
        "rows": len(body),
        "matched": matched,
        "ambiguous": ambiguous,
        "not_found": not_found,
        "duplicates": duplicates,
    }


def _person_info(person):
    email = person.access_account.email if hasattr(person, "access_account") else None
    return {"id": person.id, "name": person.name, "code": person.employee_code, "email": email}


def _same_person_name(given: str, registered: str) -> bool:
    """Confere o primeiro nome: pega planilha com matrícula trocada sem implicar com abreviações."""
    given_words, registered_words = normalize(given).split(), normalize(registered).split()
    return bool(given_words) and bool(registered_words) and given_words[0] == registered_words[0]
