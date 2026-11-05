"""Comando seed_company_staff: perfis, veículos, endereços e senha inicial pela regra de negócio."""
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.access_passwords import generate_initial_access_password
from core.management.commands.seed_company_staff import DRIVERS, GERENTES, MANAGERS
from core.models import AccessAccount, Address, Company, Person, Vehicle
from core.tests import FAST_HASHERS

PER_COMPANY = 20


def seed(**options):
    call_command("seed_company_staff", per_company=PER_COMPANY, stdout=StringIO(), **options)


@FAST_HASHERS
class SeedCompanyStaffTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alpha = Company.objects.create(name="Alpha Teste", email_domain="alpha.test")
        cls.beta = Company.objects.create(name="Beta Teste", email_domain="beta.test")

    def accounts(self, company):
        return AccessAccount.objects.filter(person__company=company).select_related("person")

    def test_cria_perfis_veiculos_e_enderecos_em_cada_empresa(self):
        seed()
        for company in (self.alpha, self.beta):
            people = Person.objects.filter(company=company)
            self.assertEqual(people.count(), PER_COMPANY)
            roles = {role: self.accounts(company).filter(role=role).count() for role in AccessAccount.Role.values}
            self.assertEqual(roles["MANAGER"], MANAGERS)
            self.assertEqual(roles["GERENTE"], GERENTES)
            self.assertEqual(roles["DRIVER"], DRIVERS)
            self.assertEqual(roles["EMPLOYEE"], PER_COMPANY - MANAGERS - GERENTES - DRIVERS)

            # Cada motorista tem exatamente um veículo da própria empresa.
            drivers = people.filter(type=Person.PersonType.DRIVER)
            self.assertEqual(Vehicle.objects.filter(company=company, driver__in=drivers).count(), DRIVERS)
            # Todos com um endereço residencial ativo em Manaus.
            addresses = Address.objects.filter(person__company=company, is_active=True)
            self.assertEqual(addresses.count(), PER_COMPANY)
            self.assertFalse(addresses.exclude(city="Manaus").exists())
            self.assertFalse(addresses.filter(neighborhood="").exists())

        # Placas e telefones não se repetem entre empresas.
        plates = list(Vehicle.objects.values_list("plate_number", flat=True))
        self.assertEqual(len(plates), len(set(plates)))
        phones = list(Person.objects.values_list("phone", flat=True))
        self.assertEqual(len(phones), len(set(phones)))

    def test_senha_inicial_segue_a_regra_e_exige_troca(self):
        seed(company=[self.alpha.id])
        for account in self.accounts(self.alpha):
            self.assertTrue(account.must_change_password)
            self.assertTrue(account.check_password(generate_initial_access_password(account.person)))
            self.assertEqual(account.email, account.person.get_auto_email())

    @override_settings(ACCESS_ACCOUNT_AUTO_INITIAL_PASSWORD="senha-fixa-de-ambiente")
    def test_reaplica_a_regra_quando_ha_senha_fixa_no_ambiente(self):
        seed(company=[self.alpha.id])
        for account in self.accounts(self.alpha):
            self.assertTrue(account.check_password(generate_initial_access_password(account.person)))
            self.assertFalse(account.check_password("senha-fixa-de-ambiente"))

    def test_rodar_de_novo_nao_duplica(self):
        seed()
        seed()
        self.assertEqual(Person.objects.count(), 2 * PER_COMPANY)
        self.assertEqual(Vehicle.objects.count(), 2 * DRIVERS)
        self.assertEqual(Address.objects.count(), 2 * PER_COMPANY)

    def test_filtro_por_empresa_e_dry_run(self):
        seed(company=[self.beta.id], dry_run=True)
        self.assertFalse(Person.objects.exists())
        seed(company=[self.beta.id])
        self.assertFalse(Person.objects.filter(company=self.alpha).exists())
        self.assertEqual(Person.objects.filter(company=self.beta).count(), PER_COMPANY)

    def test_quantidade_minima(self):
        with self.assertRaises(CommandError):
            call_command("seed_company_staff", per_company=MANAGERS + GERENTES + DRIVERS, stdout=StringIO())
