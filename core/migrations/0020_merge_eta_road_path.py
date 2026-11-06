# Junta a migração da previsão de chegada (0017_eta_road_path, criada em paralelo e já
# aplicada em produção) com a sequência principal.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_eta_road_path"),
        ("core", "0019_person_photo"),
    ]

    operations = []
