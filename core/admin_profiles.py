"""
Perfis (grupos) do Django admin, garantidos a cada `migrate` (sinal post_migrate).

* Administrador   — todas as permissões dos cadastros do Rota Certa (app core).
                    Não inclui usuários/grupos do admin: quem edita usuários pode se tornar
                    superusuário, então isso fica só com o superusuário.
* Somente leitura — apenas visualizar os cadastros do Rota Certa.

As permissões só são acrescentadas (nunca removidas), para não desfazer ajustes feitos
manualmente nesses grupos pelo admin.
"""
from django.apps import apps as global_apps

ADMIN_GROUP = "Administrador"
READ_ONLY_GROUP = "Somente leitura"


def ensure_admin_groups(sender=None, apps=global_apps, using="default", **kwargs):
    try:
        Group = apps.get_model("auth", "Group")
        Permission = apps.get_model("auth", "Permission")
    except LookupError:
        return

    core_perms = Permission.objects.using(using).filter(content_type__app_label="core")
    if not core_perms.exists():
        # Permissões ainda não criadas (ex.: migrate parcial); o próximo migrate completa.
        return

    admin_group, _ = Group.objects.using(using).get_or_create(name=ADMIN_GROUP)
    admin_group.permissions.add(*core_perms)

    read_only_group, _ = Group.objects.using(using).get_or_create(name=READ_ONLY_GROUP)
    read_only_group.permissions.add(*core_perms.filter(codename__startswith="view_"))
