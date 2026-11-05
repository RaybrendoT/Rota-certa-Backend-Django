"""
Popula cada empresa existente com mais pessoas: gestores, gerente, motoristas (com veículo) e
colaboradores com endereço residencial num bairro real de Manaus.

Distribuição padrão por empresa (--per-company 100): 3 gestores (MANAGER), 1 gerente (GERENTE),
12 motoristas (DRIVER, cada um com um veículo novo) e o resto colaboradores (EMPLOYEE).

Senhas: a conta de acesso nasce pelo signal de Person com a senha inicial da regra de negócio
(core.access_passwords: matrícula/CNH + últimos 4 dígitos do telefone) e must_change_password=True.
Se ACCESS_ACCOUNT_AUTO_INITIAL_PASSWORD estiver definido, o comando reaplica a regra mesmo assim.

Idempotente: as matrículas/CNHs/placas são determinísticas por empresa; rodar de novo só cria o que
faltar. Os colaboradores novos ficam sem rota fixa, prontos para o assistente de planejamento ou
para a fila de rotas.
"""
import random
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.access_passwords import generate_initial_access_password
from core.management.commands.seed_manaus_demo import FIRST_NAMES, LAST_NAMES
from core.models import AccessAccount, Address, Company, Person, Vehicle
from core.seed_data.manaus_bairros import BAIRROS

MANAGERS = 3
GERENTES = 1
DRIVERS = 12
GENERIC_ZIP = "69000-000"
# ~400 m de dispersão em torno do centro do bairro.
JITTER_DEG = 0.0036

VEHICLE_MODELS = [
    ("Mercedes-Benz Sprinter 416", 20),
    ("Renault Master Minibus", 16),
    ("Iveco Daily Minibus", 22),
    ("Volare W9", 28),
    ("Volare Attack 9", 32),
    ("Marcopolo Volare Fly 10", 30),
    ("Mercedes-Benz OF-1519 Torino", 44),
    ("Volkswagen 17.230 OD Caio Apache", 44),
    ("Agrale MA 9.2 Neobus", 30),
    ("Mercedes-Benz LO-916 Senior", 33),
]
PLATE_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"


class Command(BaseCommand):
    help = "Cria gestores, gerentes, motoristas (com veículos) e colaboradores em cada empresa existente."

    def add_arguments(self, parser):
        parser.add_argument("--per-company", type=int, default=100, help="Pessoas novas por empresa (padrão 100).")
        parser.add_argument("--company", type=int, action="append", help="Só esta empresa (id); pode repetir.")
        parser.add_argument("--dry-run", action="store_true", help="Mostra o que seria criado, sem gravar.")

    def handle(self, *args, **options):
        per_company = options["per_company"]
        minimum = MANAGERS + GERENTES + DRIVERS + 1
        if per_company < minimum:
            raise CommandError(f"--per-company precisa ser pelo menos {minimum}.")

        companies = Company.objects.filter(is_active=True).order_by("id")
        if options["company"]:
            companies = companies.filter(id__in=options["company"])
        if not companies:
            raise CommandError("Nenhuma empresa ativa encontrada. Cadastre as empresas antes.")

        reapply_rule = bool(getattr(settings, "ACCESS_ACCOUNT_AUTO_INITIAL_PASSWORD", ""))
        used_phones = set(Person.objects.values_list("phone", flat=True))
        used_plates = set(Vehicle.objects.values_list("plate_number", flat=True))

        for company in companies:
            plan = self._plan(company, per_company, used_phones, used_plates)
            if options["dry_run"]:
                self._report(company, plan, created=None)
                continue
            with transaction.atomic():
                created = self._apply(company, plan, reapply_rule)
            self._report(company, plan, created)

    # ------------------------------------------------------------------ plano

    def _plan(self, company, total, used_phones, used_plates):
        """Lista determinística (seed = id da empresa) de quem e o que criar."""
        rng = random.Random(f"rotacerta-staff-{company.id}")
        employees = total - MANAGERS - GERENTES - DRIVERS
        people = []
        for i in range(1, MANAGERS + 1):
            people.append(("MANAGER", f"GST{company.id}{i:02d}"))
        for i in range(1, GERENTES + 1):
            people.append(("GERENTE", f"GER{company.id}{i:02d}"))
        for i in range(1, DRIVERS + 1):
            people.append(("DRIVER", f"MND{company.id}{i:03d}"))
        for i in range(1, employees + 1):
            people.append(("EMPLOYEE", f"MNE{company.id}{i:03d}"))

        weights = [b.weight for b in BAIRROS]
        plan = []
        for role, code in people:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)} {rng.choice(LAST_NAMES)}"
            phone = self._phone(rng, used_phones)
            bairro = rng.choices(BAIRROS, weights=weights)[0]
            street = rng.choice(bairro.stops).street
            address = {
                "street": street,
                "number": str(rng.randint(10, 2500)),
                "neighborhood": bairro.name,
                "lat": Decimal(f"{bairro.lat + rng.uniform(-JITTER_DEG, JITTER_DEG):.6f}"),
                "lng": Decimal(f"{bairro.lng + rng.uniform(-JITTER_DEG, JITTER_DEG):.6f}"),
            }
            vehicle = None
            if role == "DRIVER":
                model, capacity = rng.choice(VEHICLE_MODELS)
                vehicle = {"plate": self._plate(rng, used_plates), "model": model, "capacity": capacity}
            plan.append({"role": role, "code": code, "name": name, "phone": phone, "address": address, "vehicle": vehicle})
        return plan

    @staticmethod
    def _phone(rng, used):
        while True:
            phone = f"+55929{rng.randint(0, 99_999_999):08d}"
            if phone not in used:
                used.add(phone)
                return phone

    @staticmethod
    def _plate(rng, used):
        # Placa Mercosul: LLLNLNN.
        while True:
            plate = (
                "".join(rng.choice(PLATE_LETTERS) for _ in range(3))
                + str(rng.randint(0, 9))
                + rng.choice(PLATE_LETTERS)
                + f"{rng.randint(0, 99):02d}"
            )
            if plate not in used:
                used.add(plate)
                return plate

    # --------------------------------------------------------------- gravação

    def _apply(self, company, plan, reapply_rule):
        created = {"MANAGER": 0, "GERENTE": 0, "DRIVER": 0, "EMPLOYEE": 0, "vehicles": 0, "skipped": 0}
        for item in plan:
            is_driver = item["role"] == "DRIVER"
            lookup = {"license_number": item["code"]} if is_driver else {"employee_code": item["code"]}
            if Person.objects.filter(company=company, **lookup).exists():
                created["skipped"] += 1
                continue

            person = Person(
                company=company,
                name=item["name"],
                phone=item["phone"],
                type=Person.PersonType.DRIVER if is_driver else Person.PersonType.EMPLOYEE,
                **lookup,
            )
            person.full_clean()
            person.save()  # signal cria a AccessAccount com a senha inicial da regra

            account = AccessAccount.objects.filter(person=person).first()
            if account is None:
                raise CommandError(
                    f"Conta de acesso não foi criada para {item['code']} ({company.name}): "
                    "verifique ACCESS_ACCOUNT_AUTO_CREATE e colisões de e-mail."
                )
            fields = []
            if reapply_rule:
                account.set_password(generate_initial_access_password(person))
                account.must_change_password = True
                fields += ["password_hash", "must_change_password"]
            if item["role"] in ("MANAGER", "GERENTE"):
                account.role = item["role"]
                fields.append("role")
            if fields:
                account.save(update_fields=[*fields, "updated_at"])

            addr = item["address"]
            Address.objects.create(
                person=person,
                street=addr["street"],
                number=addr["number"],
                neighborhood=addr["neighborhood"],
                city="Manaus",
                state="AM",
                zip_code=GENERIC_ZIP,
                latitude=addr["lat"],
                longitude=addr["lng"],
                source=Address.Source.PANEL,
            )

            if item["vehicle"]:
                Vehicle.objects.create(
                    company=company,
                    driver=person,
                    plate_number=item["vehicle"]["plate"],
                    model=item["vehicle"]["model"],
                    capacity=item["vehicle"]["capacity"],
                )
                created["vehicles"] += 1
            created[item["role"]] += 1
        return created

    def _report(self, company, plan, created):
        if created is None:
            counts = {}
            for item in plan:
                counts[item["role"]] = counts.get(item["role"], 0) + 1
            self.stdout.write(f"[dry-run] {company.name}: {counts}, veículos: {counts.get('DRIVER', 0)}")
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"{company.name}: gestores {created['MANAGER']}, gerentes {created['GERENTE']}, "
                f"motoristas {created['DRIVER']}, colaboradores {created['EMPLOYEE']}, "
                f"veículos {created['vehicles']}, já existiam {created['skipped']}"
            )
        )
