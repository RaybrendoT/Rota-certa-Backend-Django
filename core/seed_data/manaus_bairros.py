"""
Bairros de Manaus para o seed da empresa simulada (seed_manaus_empresa).

Coordenadas obtidas uma vez pela Geocoding API do Google (centro do bairro e endereço real
de cada ponto de parada), fixadas aqui para o seed não depender da API. weight = peso
relativo de colaboradores morando no bairro (Zonas Norte e Leste concentram a mão de obra
do Polo Industrial).
"""
from typing import NamedTuple


class StopPoint(NamedTuple):
    street: str
    number: str
    lat: float
    lng: float


class Bairro(NamedTuple):
    name: str
    zone: str
    weight: int
    lat: float
    lng: float
    stops: list[StopPoint]


COMPANY_ADDRESS = StopPoint("Avenida Buriti", "1500", -3.113516, -59.967525)

BAIRROS = [
    Bairro("Cidade Nova", "Norte", 10, -3.034000, -59.985000, [
        StopPoint("Avenida Noel Nutels", "1350", -3.030163, -59.981152),
        StopPoint("Rua Vasco de Ataíde", "S/N", -3.038409, -59.988052),
    ]),
    Bairro("Novo Aleixo", "Norte", 7, -3.062203, -59.973924, [
        StopPoint("Rua Hera Terrestre", "40", -3.057468, -59.971918),
        StopPoint("Avenida Camapuã", "S/N", -3.031797, -59.952405),
    ]),
    Bairro("Cidade de Deus", "Norte", 5, -3.018482, -59.945528, [
        StopPoint("Beco Sudão", "138", -3.025976, -59.954397),
        StopPoint("Rua Flamengo", "544", -3.016943, -59.944276),
    ]),
    Bairro("Nova Cidade", "Norte", 6, -3.012098, -59.981536, [
        StopPoint("Rua Merida", "270", -3.005686, -59.981929),
        StopPoint("Rua Malauí", "19", -2.991566, -59.972980),
    ]),
    Bairro("Santa Etelvina", "Norte", 5, -2.985747, -60.003061, [
        StopPoint("Avenida Santa Etelvina", "17", -2.991715, -60.016383),
        StopPoint("Travessa 5 Jd Fortaleza", "168", -2.983243, -60.004052),
    ]),
    Bairro("Monte das Oliveiras", "Norte", 5, -3.003949, -59.994418, [
        StopPoint("Avenida Castanheira", "82", -3.002789, -60.000001),
        StopPoint("Avenida Mulateiro", "2574", -2.994913, -59.994854),
    ]),
    Bairro("Colônia Terra Nova", "Norte", 4, -3.011043, -60.020165, [
        StopPoint("Avenida Sucupira", "700", -3.011047, -60.020156),
    ]),
    Bairro("Novo Israel", "Norte", 4, -3.027249, -60.012939, [
        StopPoint("Rua Ezequiel", "103", -3.027201, -60.012939),
    ]),
    Bairro("Colônia Santo Antônio", "Norte", 3, -3.036251, -60.010129, [
        StopPoint("Rua São João", "53", -3.036306, -60.010173),
    ]),
    Bairro("Lago Azul", "Norte", 3, -2.973380, -60.008998, [
        StopPoint("Avenida Comendador José Cruz", "13", -2.973616, -60.008841),
    ]),
    Bairro("Jorge Teixeira", "Leste", 8, -3.028352, -59.928522, [
        StopPoint("Rua Arare", "168", -3.036898, -59.928222),
        StopPoint("Avenida Itaúba", "S/N", -3.041559, -59.931567),
    ]),
    Bairro("São José Operário", "Leste", 7, -3.062580, -59.950377, [
        StopPoint("Rua Janauacá", "2", -3.074016, -59.956747),
        StopPoint("Rua Miracema", "90", -3.065139, -59.941089),
    ]),
    Bairro("Tancredo Neves", "Leste", 6, -3.054439, -59.942498, [
        StopPoint("Rua Paraíso", "41", -3.057959, -59.944805),
        StopPoint("Rua Rubelita", "144", -3.051895, -59.937492),
    ]),
    Bairro("Zumbi dos Palmares", "Leste", 5, -3.081138, -59.947915, [
        StopPoint("Rua Antônio Mota", "80", -3.080365, -59.950247),
        StopPoint("Rua Padre Marcelino", "6", -3.076227, -59.946476),
    ]),
    Bairro("Gilberto Mestrinho", "Leste", 5, -3.070321, -59.917589, [
        StopPoint("Rua Curitiba", "412", -3.066352, -59.920028),
    ]),
    Bairro("Armando Mendes", "Leste", 4, -3.090188, -59.943820, [
        StopPoint("Avenida Itacolomy", "43 A", -3.090091, -59.943842),
    ]),
    Bairro("Coroado", "Leste", 5, -3.090446, -59.981166, [
        StopPoint("Avenida Octávio Hamilton Botelho Mourão", "1064", -3.096847, -59.974983),
        StopPoint("Avenida Octávio Hamilton Botelho Mourão", "3377", -3.087420, -59.962076),
    ]),
    Bairro("Mauazinho", "Leste", 3, -3.122515, -59.939254, [
        StopPoint("Rua Bonança", "46", -3.122516, -59.939271),
    ]),
    Bairro("Colônia Antônio Aleixo", "Leste", 2, -3.087638, -59.893812, [
        StopPoint("Rua Nova República", "2", -3.087676, -59.893859),
    ]),
    Bairro("Japiim", "Sul", 5, -3.112725, -59.976814, [
        StopPoint("Rua A 18", "155", -3.118540, -59.980660),
        StopPoint("Avenida Rio Negro", "34", -3.111539, -59.970718),
    ]),
    Bairro("Petrópolis", "Sul", 4, -3.109786, -59.993997, [
        StopPoint("Rua Benjamin Constant", "94", -3.109784, -59.993925),
    ]),
    Bairro("Raiz", "Sul", 3, -3.125475, -59.998488, [
        StopPoint("Rua Tenente Jurandir", "37", -3.125548, -59.998503),
    ]),
    Bairro("São Francisco", "Sul", 2, -3.109241, -60.005713, [
        StopPoint("Rua Albérico Antunes", "24a", -3.109188, -60.005721),
    ]),
    Bairro("Crespo", "Sul", 3, -3.137009, -59.988800, [
        StopPoint("Rua Maria Andrade", "66", -3.137039, -59.988818),
    ]),
    Bairro("Educandos", "Sul", 3, -3.141209, -60.010049, [
        StopPoint("Travessa Francisco Duarte", "181", -3.141206, -60.010093),
    ]),
    Bairro("Cachoeirinha", "Sul", 3, -3.126800, -60.005713, [
        StopPoint("Avenida Carvalho Leal", "1000", -3.126857, -60.005944),
    ]),
    Bairro("Centro", "Sul", 3, -3.129896, -60.020660, [
        StopPoint("Avenida Getúlio Vargas", "626", -3.129815, -60.020472),
    ]),
    Bairro("Praça 14 de Janeiro", "Sul", 2, -3.121539, -60.012939, [
        StopPoint("Avenida Japurá", "1527", -3.121625, -60.012904),
    ]),
    Bairro("Morro da Liberdade", "Sul", 2, -3.136544, -60.001378, [
        StopPoint("Rua Doutor Martins Santana", "172", -3.136416, -60.001425),
    ]),
    Bairro("Betânia", "Sul", 2, -3.134166, -59.995598, [
        StopPoint("Rua São José", "8", -3.134314, -59.995760),
    ]),
    Bairro("Santa Luzia", "Sul", 2, -3.140500, -60.004991, [
        StopPoint("Rua Henrique Archer Pinto", "182", -3.140532, -60.005026),
    ]),
    Bairro("Parque 10 de Novembro", "Centro-Sul", 3, -3.077688, -60.008441, [
        StopPoint("Rua José Maria Moreno", "1", -3.077359, -60.008029),
    ]),
    Bairro("Aleixo", "Centro-Sul", 2, -3.099000, -59.999000, [
        StopPoint("Avenida André Araújo", "S/N", -3.100129, -59.995854),
    ]),
    Bairro("Flores", "Centro-Sul", 3, -3.054504, -60.008604, [
        StopPoint("Rua Perimetral Espanha", "122", -3.054249, -60.009210),
    ]),
    Bairro("Adrianópolis", "Centro-Sul", 1, -3.101696, -60.010049, [
        StopPoint("Avenida Umberto Calderaro", "625", -3.101760, -60.010059),
    ]),
    Bairro("Chapada", "Centro-Sul", 1, -3.096796, -60.028837, [
        StopPoint("Rua Principal", "209", -3.095773, -60.028465),
    ]),
    Bairro("São Geraldo", "Centro-Sul", 1, -3.107808, -60.026710, [
        StopPoint("Travessa Arthur Bernardes", "228", -3.107800, -60.026790),
    ]),
    Bairro("Nossa Senhora das Graças", "Centro-Sul", 1, -3.104730, -60.016675, [
        StopPoint("Travessa Turmalina", "20", -3.104700, -60.016631),
    ]),
    Bairro("Compensa", "Oeste", 5, -3.101242, -60.056925, [
        StopPoint("Alameda Josenias", "5", -3.110209, -60.064042),
        StopPoint("Avenida Brasil", "1539", -3.099929, -60.055723),
    ]),
    Bairro("Santo Agostinho", "Oeste", 2, -3.092432, -60.064976, [
        StopPoint("Rua Doutor Porfírio", "20", -3.092326, -60.064964),
    ]),
    Bairro("Vila da Prata", "Oeste", 2, -3.107257, -60.047628, [
        StopPoint("Rua São Vicente", "63", -3.107248, -60.047598),
    ]),
    Bairro("São Jorge", "Oeste", 2, -3.107622, -60.038710, [
        StopPoint("Avenida São Jorge", "4", -3.107747, -60.038564),
    ]),
    Bairro("Glória", "Oeste", 2, -3.122790, -60.035342, [
        StopPoint("Rua São Francisco", "12", -3.122862, -60.035344),
    ]),
    Bairro("Santo Antônio", "Oeste", 2, -3.115949, -60.044737, [
        StopPoint("Rua Veiga Miranda", "353", -3.115925, -60.044690),
    ]),
    Bairro("São Raimundo", "Oeste", 2, -3.124641, -60.041846, [
        StopPoint("Rua Vista Alegre", "5199", -3.124631, -60.041867),
    ]),
    Bairro("Ponta Negra", "Oeste", 1, -3.054143, -60.093967, [
        StopPoint("Avenida José Augusto Loureiro", "361", -3.054060, -60.093415),
    ]),
    Bairro("Tarumã", "Oeste", 2, -3.000125, -60.045193, [
        StopPoint("Rua C", "1704", -3.000147, -60.045072),
    ]),
    Bairro("Lírio do Vale", "Centro-Oeste", 2, -3.068889, -60.072542, [
        StopPoint("Rua Cloritta", "2300", -3.068203, -60.072802),
    ]),
    Bairro("Nova Esperança", "Centro-Oeste", 2, -3.083475, -60.059193, [
        StopPoint("Rua Nova Aurora", "165", -3.083327, -60.059284),
    ]),
    Bairro("Alvorada", "Centro-Oeste", 4, -3.075216, -60.040401, [
        StopPoint("Rua Professor Abílio Alencar", "303", -3.078560, -60.047334),
        StopPoint("Avenida Leste Oeste", "30", -3.070039, -60.037523),
    ]),
    Bairro("Redenção", "Centro-Oeste", 3, -3.053288, -60.040401, [
        StopPoint("Rua Ibis Brilhante", "7", -3.053057, -60.040237),
    ]),
    Bairro("Planalto", "Centro-Oeste", 2, -3.064695, -60.054856, [
        StopPoint("Avenida Dublin", "13", -3.064603, -60.054905),
    ]),
    Bairro("Da Paz", "Centro-Oeste", 3, -3.057317, -60.028837, [
        StopPoint("Rua Carlos Natrott", "9", -3.057200, -60.028918),
    ]),
    Bairro("Dom Pedro", "Centro-Oeste", 2, -3.084548, -60.043750, [
        StopPoint("Avenida Domingos Jorge Velho", "200a", -3.084622, -60.043654),
    ]),
]
