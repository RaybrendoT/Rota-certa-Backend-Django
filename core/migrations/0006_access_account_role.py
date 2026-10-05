from django.db import migrations, models


def backfill_roles(apps, schema_editor):
    """Contas existentes de motoristas passam a DRIVER; as demais ficam EMPLOYEE (default)."""
    AccessAccount = apps.get_model("core", "AccessAccount")
    AccessAccount.objects.using(schema_editor.connection.alias).filter(
        person__type="DRIVER"
    ).update(role="DRIVER")


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0005_driver_route_stops'),
    ]

    operations = [
        migrations.AddField(
            model_name='accessaccount',
            name='role',
            field=models.CharField(choices=[('DRIVER', 'Motorista'), ('EMPLOYEE', 'Funcionário'), ('MANAGER', 'Gestor')], db_index=True, default='EMPLOYEE', help_text='Perfil de acesso. DRIVER só para pessoa DRIVER; EMPLOYEE e MANAGER só para pessoa EMPLOYEE.', max_length=20),
        ),
        migrations.RunPython(backfill_roles, migrations.RunPython.noop),
    ]
