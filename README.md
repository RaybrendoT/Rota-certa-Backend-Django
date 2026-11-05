# 🚍 Rota Certa — Backend

API REST para gerenciamento e monitoramento de transporte corporativo, desenvolvida com **Python, Django e Django REST Framework**.

O projeto foi desenvolvido com foco em organização de domínio, validação de regras de negócio, integridade dos dados e disponibilização de uma API REST escalável para gerenciamento de empresas, funcionários, motoristas, veículos, rotas, passageiros, pontos de encontro, horários e eventos de operação.

---

## 📋 Sumário

* [Sobre o projeto](#-sobre-o-projeto)
* [Principais funcionalidades](#-principais-funcionalidades)
* [Tecnologias utilizadas](#-tecnologias-utilizadas)
* [Arquitetura](#-arquitetura)
* [Modelo de domínio](#-modelo-de-domínio)
* [Regras de negócio](#-regras-de-negócio)
* [Estrutura do projeto](#-estrutura-do-projeto)
* [Pré-requisitos](#-pré-requisitos)
* [Instalação](#-instalação)
* [Configuração das variáveis de ambiente](#-configuração-das-variáveis-de-ambiente)
* [Executando o projeto](#-executando-o-projeto)
* [Banco de dados](#-banco-de-dados)
* [Autenticação](#-autenticação)
* [Documentação da API](#-documentação-da-api)
* [Endpoints](#-endpoints)
* [Exemplos de utilização](#-exemplos-de-utilização)
* [Validações](#-validações)
* [Testes](#-testes)
* [Boas práticas adotadas](#-boas-práticas-adotadas)
* [Próximos passos](#-próximos-passos)
* [Autor](#-autor)

---

## 🚀 Sobre o projeto

O **Rota Certa Backend** é uma API REST destinada ao gerenciamento de operações de transporte corporativo.

A aplicação centraliza informações relacionadas a:

* empresas;
* funcionários;
* motoristas;
* veículos;
* rotas;
* passageiros;
* eventos de percurso;
* pontos de encontro;
* endereços;
* horários de operação.

A API foi estruturada utilizando os princípios REST e possui regras de negócio implementadas principalmente na camada de serializers, garantindo que os dados recebidos pela API respeitem as regras do domínio.

---

## ✨ Principais funcionalidades

### 🏢 Empresas

Permite cadastrar e administrar empresas responsáveis pelas operações de transporte.

Operações disponíveis:

* criação;
* consulta;
* atualização;
* exclusão;
* ativação/desativação.

---

### 👤 Pessoas

O sistema utiliza uma entidade `Person` para representar diferentes tipos de pessoas relacionadas à operação.

Atualmente existem dois tipos:

```text
EMPLOYEE
DRIVER
```

A diferenciação é realizada por meio do campo:

```text
type
```

Essa abordagem evita a criação de tabelas separadas para funcionários e motoristas.

---

### 🚗 Motoristas

Permite cadastrar motoristas vinculados a uma empresa.

Motoristas possuem informações específicas, como:

* nome;
* telefone;
* número da CNH;
* empresa;
* status de atividade.

Somente pessoas classificadas como `DRIVER` podem ser associadas a veículos.

---

### 👨‍💼 Funcionários

Funcionários representam os passageiros que utilizam o transporte corporativo.

Cada funcionário pode possuir:

* nome;
* telefone;
* código funcional;
* empresa;
* status.

Somente pessoas classificadas como `EMPLOYEE` podem ser adicionadas como passageiros de uma rota.

---

### 🚐 Veículos

Permite cadastrar os veículos utilizados nas operações.

Informações principais:

* placa;
* modelo;
* ano;
* motorista responsável;
* empresa;
* status.

Existe uma validação de domínio garantindo que somente uma pessoa do tipo `DRIVER` possa ser associada como motorista.

---

### 🛣️ Rotas

Representa uma operação de transporte entre um ponto de origem e um destino.

Cada rota possui:

* empresa;
* nome;
* origem;
* destino;
* status;
* data de criação;
* data de atualização;
* status de atividade.

Os estados possíveis são:

```text
planned
in_progress
completed
cancelled
```

---

### 👥 Passageiros da rota

O relacionamento entre funcionários e rotas é representado por `RoutePassenger`.

Isso permite associar diferentes funcionários a uma determinada rota.

Regra principal:

> Somente pessoas do tipo `EMPLOYEE` podem ser adicionadas como passageiros.

---

### 📍 Eventos de rota

Os eventos permitem registrar acontecimentos durante a operação de uma rota.

Tipos disponíveis:

```text
delay
incident
stop
departure
arrival
```

Os eventos também podem armazenar informações de geolocalização:

```text
latitude
longitude
```

Isso possibilita registrar onde determinado evento ocorreu.

---

### 📌 Pontos de encontro

Os pontos de encontro são representados por `MeetingPoint`.

Um ponto de encontro associa:

```text
Route → Address
```

permitindo definir locais onde os passageiros podem embarcar ou desembarcar.

---

### 🕐 Horários

A entidade `Schedule` permite definir os horários planejados de uma rota.

São armazenados:

* horário planejado de início;
* horário planejado de término;
* rota relacionada.

---

# 🧩 Tecnologias utilizadas

| Tecnologia            | Utilização                              |
| --------------------- | --------------------------------------- |
| Python                | Linguagem principal                     |
| Django                | Framework web                           |
| Django REST Framework | Construção da API REST                  |
| PostgreSQL            | Banco de dados relacional               |
| JWT                   | Autenticação                            |
| Swagger / OpenAPI     | Documentação da API                     |
| Django ORM            | Persistência e relacionamento dos dados |
| Unit Tests            | Testes automatizados                    |

---

# 🏗️ Arquitetura

A aplicação utiliza uma arquitetura baseada no ecossistema Django e Django REST Framework.

De forma simplificada:

```text
                    ┌─────────────────────┐
                    │     Cliente/API     │
                    │ Web / Mobile / App  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │     Django REST     │
                    │      Framework      │
                    └──────────┬──────────┘
                               │
             ┌─────────────────┼─────────────────┐
             ▼                 ▼                 ▼
        Serializers         Views             URLs
             │                 │
             └────────┬────────┘
                      ▼
                Django Models
                      │
                      ▼
                Django ORM
                      │
                      ▼
                PostgreSQL
```

A camada de serializers possui papel importante na aplicação, pois além de serializar e desserializar os dados, também concentra diversas regras de negócio.

---

# 🧠 Modelo de domínio

## Company

Representa uma empresa.

```text
Company
├── id
├── name
├── created_at
├── updated_at
└── is_active
```

---

## Person

Representa funcionários e motoristas.

```text
Person
├── id
├── company
├── name
├── phone
├── type
├── employee_code
├── license_number
├── created_at
├── updated_at
└── is_active
```

Tipos:

```text
EMPLOYEE
DRIVER
```

---

## Vehicle

Representa um veículo pertencente à operação.

```text
Vehicle
├── id
├── company
├── plate_number
├── model
├── year
├── driver
├── created_at
├── updated_at
└── is_active
```

---

## Route

Representa uma rota de transporte.

```text
Route
├── id
├── company
├── name
├── origin
├── destination
├── status
├── created_at
├── updated_at
└── is_active
```

---

## RoutePassenger

Relaciona funcionários às rotas.

```text
RoutePassenger
├── id
├── route
└── person
```

---

## RouteEvent

Registra acontecimentos durante uma rota.

```text
RouteEvent
├── id
├── route
├── event_type
├── latitude
├── longitude
├── description
├── created_at
└── updated_at
```

---

## Address

Representa um endereço.

```text
Address
├── id
├── street
├── city
├── state
├── zip_code
├── created_at
└── updated_at
```

---

## MeetingPoint

Relaciona um endereço a uma rota.

```text
MeetingPoint
├── id
├── route
├── address
├── created_at
└── updated_at
```

---

## Schedule

Representa o horário planejado da rota.

```text
Schedule
├── id
├── route
├── planned_start_time
├── planned_end_time
├── created_at
└── updated_at
```

---

# 🧬 Estratégia TPH — Person

Uma das decisões arquiteturais do projeto é a utilização da estratégia **Single Table / TPH (Table Per Hierarchy)**.

Em vez de possuir:

```text
Employee
Driver
```

como tabelas independentes, ambas as representações utilizam a mesma tabela:

```text
Person
```

A diferenciação ocorre através de:

```python
type
```

Exemplo:

```text
Person
────────────────────────────
id
name
phone
type
employee_code
license_number
```

Onde:

```text
EMPLOYEE
    → employee_code obrigatório
    → license_number não permitido

DRIVER
    → license_number obrigatório
    → employee_code não permitido
```

### Vantagens

Essa abordagem proporciona:

* modelo de dados mais simples;
* redução de duplicidade;
* relacionamento centralizado;
* manutenção facilitada;
* possibilidade de expansão para novos tipos de pessoa;
* regras de domínio centralizadas.

---

# 📜 Regras de negócio

## Pessoa

Uma pessoa deve possuir obrigatoriamente um tipo.

### Funcionário

```text
type = EMPLOYEE
```

Requer:

```text
employee_code
```

Não permite:

```text
license_number
```

---

### Motorista

```text
type = DRIVER
```

Requer:

```text
license_number
```

Não permite:

```text
employee_code
```

---

## Veículo

Um veículo só pode possuir como motorista uma pessoa cujo tipo seja:

```text
DRIVER
```

---

## Passageiro

Uma pessoa somente pode ser adicionada como passageiro quando:

```text
type = EMPLOYEE
```

---

## Eventos

A latitude deve respeitar:

```text
-90 ≤ latitude ≤ 90
```

A longitude deve respeitar:

```text
-180 ≤ longitude ≤ 180
```

Para eventos críticos, a descrição é obrigatória.

Eventos críticos:

```text
delay
incident
```

---

# 🗂️ Estrutura do projeto

Estrutura principal do repositório:

```text
Rota-certa-Backend-Django/
│
├── config/
│   └── Configurações do projeto Django
│
├── core/
│   └── Aplicação principal e domínio
│
├── docs/
│   └── Documentação adicional
│
├── .env.example
├── .gitignore
├── PT-README.md
├── README.md
├── core_data.json
├── data.json
├── main.py
├── manage.py
└── requirements.txt
```

---

# ⚙️ Pré-requisitos

Antes de executar o projeto, certifique-se de possuir:

* Python 3.10 ou superior;
* PostgreSQL;
* Git;
* pip;
* ambiente virtual Python.

Opcionalmente:

* Postman;
* Insomnia;
* Docker;
* DBeaver ou pgAdmin.

---

# 📥 Instalação

Clone o repositório:

```bash
git clone https://github.com/RaybrendoT/Rota-certa-Backend-Django.git
```

Entre no diretório:

```bash
cd Rota-certa-Backend-Django
```

---

## 🐍 Criando o ambiente virtual

### Windows

```powershell
python -m venv .venv
```

Ative:

```powershell
.venv\Scripts\activate
```

### Linux/macOS

```bash
python3 -m venv .venv
```

Ative:

```bash
source .venv/bin/activate
```

---

# 📦 Instalando dependências

Com o ambiente virtual ativado:

```bash
pip install -r requirements.txt
```

---

# 🔐 Configuração das variáveis de ambiente

O projeto disponibiliza um arquivo:

```text
.env.example
```

Faça uma cópia para:

```text
.env
```

Exemplo:

```env
DEBUG=True

SECRET_KEY=sua-chave-secreta

DATABASE_NAME=rota_certa
DATABASE_USER=postgres
DATABASE_PASSWORD=sua-senha
DATABASE_HOST=localhost
DATABASE_PORT=5432
```

> Os nomes exatos das variáveis devem acompanhar a configuração presente no projeto. Nunca versione credenciais reais, senhas ou chaves privadas no Git.

---

# 🗄️ Configuração do banco de dados

Crie o banco PostgreSQL utilizado pela aplicação.

Depois execute as migrações:

```bash
python manage.py makemigrations
```

```bash
python manage.py migrate
```

---

# 👤 Criando um superusuário

Para acessar o Django Admin:

```bash
python manage.py createsuperuser
```

Informe:

```text
Username:
Email:
Password:
```

---

# ▶️ Executando o projeto

Inicie o servidor:

```bash
python manage.py runserver
```

Por padrão, a aplicação estará disponível em:

```text
http://127.0.0.1:8000/
```

---

# 📚 Documentação da API

O projeto utiliza **Swagger/OpenAPI** para facilitar o desenvolvimento e testes da API.

A documentação pode ser disponibilizada através das rotas configuradas no projeto.

Exemplo:

```text
http://127.0.0.1:8000/swagger/
```

Caso a rota tenha sido configurada com outro caminho, consulte:

```text
config/urls.py
```

A documentação permite visualizar:

* endpoints;
* métodos HTTP;
* parâmetros;
* payloads;
* respostas;
* códigos HTTP;
* modelos utilizados pela API.

---

# 🔑 Autenticação JWT

A API utiliza autenticação baseada em **JSON Web Token (JWT)**.

O fluxo esperado é:

```text
Login
  │
  ▼
Access Token + Refresh Token
  │
  ▼
Requisições autenticadas
  │
  ▼
API
```

Nas requisições protegidas, o token deve ser enviado no header:

```http
Authorization: Bearer <access_token>
```

---

# 🔌 Endpoints

## Empresas

```http
GET    /companies/
POST   /companies/
GET    /companies/{id}/
PUT    /companies/{id}/
DELETE /companies/{id}/
```

---

## Funcionários

```http
GET    /employees/
POST   /employees/
GET    /employees/{id}/
PUT    /employees/{id}/
DELETE /employees/{id}/
```

---

## Motoristas

```http
GET    /drivers/
POST   /drivers/
GET    /drivers/{id}/
PUT    /drivers/{id}/
DELETE /drivers/{id}/
```

---

## Veículos

```http
GET    /vehicles/
POST   /vehicles/
GET    /vehicles/{id}/
PUT    /vehicles/{id}/
DELETE /vehicles/{id}/
```

---

## Rotas

```http
GET    /routes/
POST   /routes/
GET    /routes/{id}/
PUT    /routes/{id}/
DELETE /routes/{id}/
```

---

## Passageiros

```http
GET    /route-passengers/
POST   /route-passengers/
DELETE /route-passengers/{id}/
```

---

## Eventos

```http
GET  /route-events/
POST /route-events/
```

---

## Pontos de encontro

```http
GET  /meeting-points/
POST /meeting-points/
```

---

## Horários

```http
GET  /schedules/
POST /schedules/
```

---

# 🧪 Exemplos de utilização

## Criando um funcionário

Exemplo conceitual:

```json
{
    "name": "João da Silva",
    "phone": "92999999999",
    "type": "EMPLOYEE",
    "employee_code": "EMP001"
}
```

---

## Criando um motorista

```json
{
    "name": "Carlos Oliveira",
    "phone": "92988888888",
    "type": "DRIVER",
    "license_number": "12345678900"
}
```

---

## Criando uma rota

```json
{
    "name": "Rota Centro",
    "origin": "Distrito Industrial",
    "destination": "Centro",
    "status": "planned"
}
```

---

## Registrando um evento

```json
{
    "route": "UUID_DA_ROTA",
    "event_type": "delay",
    "latitude": -3.119,
    "longitude": -60.021,
    "description": "Atraso causado pelo trânsito."
}
```

---

# 🛡️ Validações

A aplicação possui múltiplas camadas de proteção da integridade dos dados.

Entre elas:

```text
Model validation
      ↓
full_clean()
      ↓
Serializer validation
      ↓
Business rules
      ↓
Database
```

São utilizadas estratégias como:

* `validate()`;
* `validate_<field>()`;
* `full_clean()`;
* validações nos serializers;
* constraints e relacionamentos do banco;
* validação de tipos;
* validação geográfica.

---

# 🧪 Testes

O projeto possui estrutura voltada para testes de:

* models;
* serializers;
* endpoints;
* regras de negócio.

Para executar os testes:

```bash
python manage.py test
```

Ou, caso sejam utilizados testes específicos:

```bash
python manage.py test core
```

---

## CI/CD e deploy

O workflow [`.github/workflows/ci-cd.yml`](.github/workflows/ci-cd.yml) roda em todo pull request e a cada push na `main`:
**lint** (`ruff check`) → **testes** (num Postgres 16) → **build** da imagem → **push** no GHCR → **deploy** na EC2.
Em PR ele só constrói a imagem, sem publicar nem fazer deploy.

Antes do push, rode o lint localmente:

```bash
pip install ruff==0.17.3
ruff check .          # ruff check --fix . arruma a ordem dos imports e afins
```

O guia do servidor, dos secrets do GitHub e do rollback está em [`deploy/README.md`](deploy/README.md).

---

# 🔍 Testando com Postman

Uma forma simples de testar a API é utilizando o Postman.

Fluxo recomendado:

```text
1. Autenticar
      ↓
2. Obter access_token
      ↓
3. Configurar Authorization
      ↓
4. Criar empresa
      ↓
5. Criar funcionário
      ↓
6. Criar motorista
      ↓
7. Criar veículo
      ↓
8. Criar rota
      ↓
9. Adicionar passageiros
      ↓
10. Registrar eventos
```

---

# 🧹 Comandos úteis

Executar servidor:

```bash
python manage.py runserver
```

Criar migrações:

```bash
python manage.py makemigrations
```

Aplicar migrações:

```bash
python manage.py migrate
```

Criar superusuário:

```bash
python manage.py createsuperuser
```

Abrir shell Django:

```bash
python manage.py shell
```

Executar testes:

```bash
python manage.py test
```

Verificar problemas do projeto:

```bash
python manage.py check
```

---

# 📊 Fluxo geral da aplicação

```text
                    EMPRESA
                       │
          ┌────────────┼────────────┐
          │            │            │
          ▼            ▼            ▼
      FUNCIONÁRIOS  MOTORISTAS   VEÍCULOS
          │            │
          │            └──────┐
          │                   │
          ▼                   ▼
     PASSAGEIROS           MOTORISTA
          │                   │
          └────────┐   ┌──────┘
                   ▼   ▼
                    ROTA
                     │
          ┌──────────┼──────────┐
          │          │          │
          ▼          ▼          ▼
      HORÁRIOS   PONTOS      EVENTOS
                 ENCONTRO
                     │
                     ▼
                 ENDEREÇO
```

---

# 🎯 Objetivos técnicos

O projeto foi estruturado buscando demonstrar conhecimentos em:

* desenvolvimento de APIs REST;
* Django;
* Django REST Framework;
* modelagem relacional;
* PostgreSQL;
* autenticação JWT;
* serializers;
* validação de regras de negócio;
* relacionamentos entre entidades;
* tratamento de dados geográficos;
* testes automatizados;
* documentação de APIs;
* organização de projetos Python.

---

# 🔮 Próximos passos

Algumas evoluções que podem ser incorporadas futuramente:

* [ ] Paginação dos endpoints;
* [ ] Filtros avançados;
* [ ] Ordenação;
* [ ] Busca textual;
* [ ] Controle de permissões por perfil;
* [ ] Histórico de alterações;
* [ ] Logs de auditoria;
* [ ] Rastreamento em tempo real;
* [ ] Integração com mapas;
* [ ] WebSockets;
* [ ] Redis;
* [ ] Celery para tarefas assíncronas;
* [x] Docker;
* [x] CI/CD com GitHub Actions;
* [ ] Testes de integração;
* [ ] Cobertura de testes;
* [ ] Deploy em ambiente cloud.

---

# 🔒 Segurança

Algumas recomendações para utilização em produção:

* não versionar o arquivo `.env`;
* utilizar `DEBUG=False`;
* utilizar uma `SECRET_KEY` segura;
* configurar corretamente `ALLOWED_HOSTS`;
* utilizar HTTPS;
* proteger endpoints com autenticação;
* configurar permissões adequadas;
* utilizar banco de dados com credenciais próprias;
* evitar exposição de informações sensíveis nos logs.

---

# 📌 Status do projeto

**Em desenvolvimento 🚧**

O backend possui a estrutura principal da API e as entidades fundamentais para gerenciamento de transporte corporativo.

Novas funcionalidades e melhorias podem ser adicionadas conforme a evolução do sistema.

---

# 👨‍💻 Autor

Desenvolvido por **Raybrendo Trindade** e **Jose Eduardo**.

GitHub:

https://github.com/RaybrendoT
https://github.com/zezinjr

Repositório:

https://github.com/RaybrendoT/Rota-certa-Backend-Django

---

## 📄 Licença

Este projeto é destinado a fins educacionais, acadêmicos e de desenvolvimento de software.

Consulte o repositório para informações adicionais sobre a licença e utilização do código.
