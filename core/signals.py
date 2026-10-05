import logging

from django.conf import settings
from django.db import IntegrityError
from django.db.models.signals import post_save
from django.dispatch import receiver

from .access_passwords import generate_initial_access_password
from .models import AccessAccount, Company, Person

logger = logging.getLogger(__name__)


def sync_access_account_role_for_person(person):
    """
    Mantém o perfil coerente com o tipo da pessoa: DRIVER só para motorista.
    Se o tipo mudar, o perfil acompanha (um gestor que vira motorista deixa de ser gestor).
    """
    try:
        account = AccessAccount.objects.get(person_id=person.pk)
    except AccessAccount.DoesNotExist:
        return

    is_driver = person.type == Person.PersonType.DRIVER
    if is_driver and account.role != AccessAccount.Role.DRIVER:
        new_role = AccessAccount.Role.DRIVER
    elif not is_driver and account.role == AccessAccount.Role.DRIVER:
        new_role = AccessAccount.Role.EMPLOYEE
    else:
        return

    logger.info(
        "AccessAccount id=%s: perfil %s -> %s (tipo da pessoa mudou). person_id=%s.",
        account.pk,
        account.role,
        new_role,
        person.pk,
    )
    account.person = person
    account.role = new_role
    account.save(update_fields=["role", "updated_at"])


def sync_access_account_email_for_person(person):
    """
    Atualiza AccessAccount.email para coincidir com get_auto_email() da pessoa,
    desde que não haja colisão com outra conta.
    """
    if not person.is_active:
        return
    try:
        account = AccessAccount.objects.get(person_id=person.pk)
    except AccessAccount.DoesNotExist:
        return

    new_email = person.get_auto_email()
    if not new_email:
        logger.warning(
            "AccessAccount id=%s: person_id=%s sem employee_code/license_number; "
            "e-mail não sincronizado.",
            account.pk,
            person.pk,
        )
        return

    if account.email == new_email:
        return

    if AccessAccount.objects.filter(email=new_email).exclude(pk=account.pk).exists():
        logger.warning(
            "AccessAccount id=%s: não alterado para %s (e-mail já usado por outra conta). "
            "person_id=%s.",
            account.pk,
            new_email,
            person.pk,
        )
        return

    account.email = new_email
    try:
        account.save(update_fields=["email", "updated_at"])
    except IntegrityError:
        logger.warning(
            "AccessAccount id=%s: falha ao definir e-mail %s (integridade). person_id=%s.",
            account.pk,
            new_email,
            person.pk,
            exc_info=True,
        )


def _create_access_account_if_missing(instance):
    if AccessAccount.objects.filter(person_id=instance.pk).exists():
        return

    email = instance.get_auto_email()
    if not email:
        return

    if AccessAccount.objects.filter(email=email).exclude(person_id=instance.pk).exists():
        logger.warning(
            "AccessAccount automático não criado: e-mail %s já usado por outra conta "
            "(person_id=%s).",
            email,
            instance.pk,
        )
        return

    acc = AccessAccount(
        person=instance,
        email=email,
        role=AccessAccount.default_role_for(instance),
        must_change_password=True,
    )
    initial = getattr(settings, "ACCESS_ACCOUNT_AUTO_INITIAL_PASSWORD", "") or ""
    if initial:
        acc.set_password(initial)
    else:
        acc.set_password(generate_initial_access_password(instance))

    try:
        acc.save()
    except IntegrityError:
        logger.warning(
            "AccessAccount automático não criado: colisão ao salvar e-mail %s "
            "(person_id=%s).",
            email,
            instance.pk,
            exc_info=True,
        )


@receiver(
    post_save,
    sender=Person,
    dispatch_uid="core.person_auto_access_account",
)
def person_post_save_access_account(sender, instance, **kwargs):
    if not instance.is_active:
        return

    if getattr(settings, "ACCESS_ACCOUNT_AUTO_CREATE", True):
        _create_access_account_if_missing(instance)

    sync_access_account_role_for_person(instance)

    if getattr(settings, "ACCESS_ACCOUNT_SYNC_EMAIL", True):
        sync_access_account_email_for_person(instance)


@receiver(
    post_save,
    sender=Company,
    dispatch_uid="core.company_sync_access_emails",
)
def company_post_save_sync_access_emails(sender, instance, **kwargs):
    if not getattr(settings, "ACCESS_ACCOUNT_SYNC_EMAIL", True):
        return

    persons = Person.objects.filter(
        company=instance,
        is_active=True,
        access_account__isnull=False,
    ).select_related("company")

    for person in persons:
        sync_access_account_email_for_person(person)
