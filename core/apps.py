from django.apps import AppConfig
from django.db.models.signals import post_migrate


class CoreConfig(AppConfig):
    default = True
    name = "core"
    verbose_name = "Rota Certa"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        import core.signals  # noqa: F401
        from core.admin_profiles import ensure_admin_groups

        # Roda depois do post_migrate do auth (que cria as permissões), pois "core"
        # vem depois de django.contrib.auth em INSTALLED_APPS.
        post_migrate.connect(ensure_admin_groups, sender=self, dispatch_uid="core.ensure_admin_groups")
