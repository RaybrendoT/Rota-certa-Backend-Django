"""
Gera um CSV fictício no formato do importar_csv: sede da empresa, colaboradores com endereço em
bairros reais de Manaus, gestor, gerente e motoristas com veículo. Determinístico (mesma semente,
mesmo arquivo), para dar para repetir a demonstração.

Ex.: python manage.py gerar_csv_exemplo --saida mostratech.csv
     python manage.py importar_csv mostratech.csv --empresa MostraTech --dominio mostratech.com.br
"""
import csv
import io
import random

from django.core.management.base import BaseCommand

from core.importacao_csv import COLUMNS
from core.management.commands.seed_company_staff import PLATE_LETTERS, VEHICLE_MODELS
from core.management.commands.seed_manaus_demo import FIRST_NAMES, LAST_NAMES
from core.seed_data.manaus_bairros import BAIRROS, COMPANY_ADDRESS

JITTER_DEG = 0.0036  # ~400 m em torno do centro do bairro


class Command(BaseCommand):
    help = "Gera um CSV fictício de funcionários (colaboradores, gestor, gerente e motoristas) para o importar_csv."

    def add_arguments(self, parser):
        parser.add_argument("--empresa", default="MostraTech")
        parser.add_argument("--dominio", default="mostratech.com.br")
        parser.add_argument("--prefixo", default="MT", help="Prefixo das matrículas (padrão MT).")
        parser.add_argument("--colaboradores", type=int, default=500)
        parser.add_argument("--motoristas", type=int, default=25)
        parser.add_argument("--gestores", type=int, default=1)
        parser.add_argument("--gerentes", type=int, default=1)
        parser.add_argument("--semente", default="mostratech")
        parser.add_argument("--saida", help="Arquivo de saída. Padrão: imprime na tela.")

    def handle(self, *args, **o):
        rng = random.Random(o["semente"])
        used_phones, used_plates, used_cnh = set(), set(), set()
        weights = [b.weight for b in BAIRROS]
        rows = [self._row(
            perfil="EMPRESA", nome=o["empresa"], logradouro=COMPANY_ADDRESS.street, numero=COMPANY_ADDRESS.number,
            bairro="Distrito Industrial I", cidade="Manaus", uf="AM", cep="69075-000",
            latitude=f"{COMPANY_ADDRESS.lat:.6f}", longitude=f"{COMPANY_ADDRESS.lng:.6f}",
        )]

        def person(perfil, code_col, code, with_address):
            phone = self._phone(rng, used_phones)
            row = {"perfil": perfil, "nome": self._name(rng), code_col: code, "telefone": phone,
                   "email": f"{code.lower()}@{o['dominio']}"}
            if with_address:
                bairro = rng.choices(BAIRROS, weights=weights)[0]
                row.update(
                    logradouro=rng.choice(bairro.stops).street, numero=str(rng.randint(10, 2500)),
                    bairro=bairro.name, cidade="Manaus", uf="AM", cep="69000-000",
                    latitude=f"{bairro.lat + rng.uniform(-JITTER_DEG, JITTER_DEG):.6f}",
                    longitude=f"{bairro.lng + rng.uniform(-JITTER_DEG, JITTER_DEG):.6f}",
                )
            return row

        p = o["prefixo"].upper()
        for i in range(1, o["gestores"] + 1):
            rows.append(self._row(**person("GESTOR", "matricula", f"{p}G{i:03d}", True)))
        for i in range(1, o["gerentes"] + 1):
            rows.append(self._row(**person("GERENTE", "matricula", f"{p}R{i:03d}", True)))
        for _ in range(o["motoristas"]):
            cnh = self._unique(lambda: f"{rng.randint(10**10, 10**11 - 1)}", used_cnh)
            row = person("MOTORISTA", "cnh", cnh, False)
            model, capacity = rng.choice(VEHICLE_MODELS)
            row.update(placa=self._plate(rng, used_plates), modelo_veiculo=model, capacidade=str(capacity))
            rows.append(self._row(**row))
        for i in range(1, o["colaboradores"] + 1):
            rows.append(self._row(**person("COLABORADOR", "matricula", f"{p}{i:04d}", True)))

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        if o["saida"]:
            with open(o["saida"], "w", newline="", encoding="utf-8") as fh:
                fh.write(buffer.getvalue())
            self.stdout.write(self.style.SUCCESS(f"{len(rows) - 1} pessoas + sede em {o['saida']}"))
        else:
            self.stdout.write(buffer.getvalue(), ending="")

    @staticmethod
    def _row(**values):
        return {c: values.get(c, "") for c in COLUMNS}

    @staticmethod
    def _name(rng):
        return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)} {rng.choice(LAST_NAMES)}"

    @staticmethod
    def _unique(make, used):
        while True:
            value = make()
            if value not in used:
                used.add(value)
                return value

    def _phone(self, rng, used):
        return self._unique(lambda: f"(92) 9{rng.randint(8000, 9999)}-{rng.randint(0, 9999):04d}", used)

    def _plate(self, rng, used):
        def make():
            return ("".join(rng.choice(PLATE_LETTERS) for _ in range(3)) + str(rng.randint(0, 9))
                    + rng.choice(PLATE_LETTERS) + f"{rng.randint(0, 99):02d}")
        return self._unique(make, used)
