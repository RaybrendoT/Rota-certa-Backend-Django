🚍 Sistema de Gestão de Rotas e Passageiros (API)
📌 Descrição

Você foi contratado para desenvolver a API de um sistema corporativo de gestão de transporte e rotas.

O sistema permite que empresas cadastrem suas rotas, motoristas, funcionários (passageiros), pontos de encontro, horários de operação e registrem eventos relacionados à execução das rotas.

A API foi desenvolvida utilizando Django REST Framework e é testável via Postman.

🎯 Objetivo

Fornecer uma API RESTful para:

Cadastro e gerenciamento de empresas

Organização de rotas e horários

Associação de motoristas e passageiros às rotas

Definição de pontos de encontro com endereço e geolocalização

Registro de eventos operacionais das rotas

🏗 Arquitetura Atual
👤 Modelo Person (Estratégia TPH – Tabela Única)

O sistema utiliza um único modelo Person para representar:

Motoristas

Funcionários (Passageiros)

A diferenciação é feita por meio do campo:

type = "EMPLOYEE" | "DRIVER"
🔒 Regras de Validação
Tipo	Obrigatório	Proibido
EMPLOYEE	employee_code	license_number
DRIVER	license_number	employee_code

As regras são aplicadas diretamente nos serializers.

Essa abordagem:

Simplifica o banco de dados

Reduz duplicidade

Centraliza validações

Facilita manutenção futura

✅ Funcionalidades
🏢 Empresas

Criar, listar, atualizar e desativar empresas

Soft delete via is_active = false

UUID como chave primária

👤 Pessoas

Cadastro de pessoas contendo:

Nome

Telefone

Empresa

Tipo (EMPLOYEE ou DRIVER)

Código de funcionário (quando EMPLOYEE)

Número de licença (quando DRIVER)

🚗 Motoristas

Implementados como Person com type = DRIVER

Obrigatório possuir license_number

Não podem possuir employee_code

Podem ser vinculados a veículos

🧑‍💼 Funcionários (Passageiros)

Implementados como Person com type = EMPLOYEE

Obrigatório possuir employee_code

Não podem possuir license_number

Podem ser associados a rotas como passageiros

🚙 Veículos

Pertencem a uma empresa

Podem ter um motorista vinculado

Apenas pessoas do tipo DRIVER podem ser associadas

🛣 Rotas

Criar, listar, atualizar e desativar rotas

Pertencem a uma empresa

Podem ter:

Um motorista ativo

Múltiplos passageiros

Múltiplos pontos de encontro

Múltiplos horários

⏰ Horários (Schedules)

Definição de dias da semana

Horário de início e fim

Podem ser ativados ou desativados

📍 Pontos de Encontro

Vinculados a uma rota

Devem possuir endereço válido

Representam locais físicos de embarque/desembarque

🏠 Endereços

Cadastro completo contendo:

Rua

Número

Cidade

Estado

CEP

Latitude

Longitude

📑 Eventos de Rota

Registro de eventos operacionais:

Tipos de eventos:

departure (saída)

arrival (chegada)

stop (parada)

delay (atraso)

incident (ocorrência)

Campos:

Rota

Tipo de evento

Latitude

Longitude

Descrição

Timestamp

🔎 Validações

Latitude deve estar entre -90 e 90

Longitude deve estar entre -180 e 180

Descrição é obrigatória para:

delay

incident

🚫 Fora do Escopo

O sistema:

Não possui autenticação

Não possui controle de acesso

Não envia notificações

Não possui dashboards

Não realiza rastreamento GPS em tempo real

Não permite upload de arquivos

Não mantém histórico de auditoria

📐 Regras de Negócio

Uma empresa pode possuir várias rotas.

Uma rota pertence a apenas uma empresa.

Uma rota pode ter apenas um motorista ativo por vez.

Uma rota pode ter múltiplos passageiros.

Um funcionário pode estar em várias rotas.

Um motorista pode estar vinculado a vários veículos.

Apenas EMPLOYEE pode ser passageiro.

Apenas DRIVER pode ser motorista de veículo.

Pontos de encontro devem possuir endereço válido.

Exclusão lógica aplicada via is_active.

🧱 Entidades Principais

Company

Person (EMPLOYEE / DRIVER)

Vehicle

Route

RoutePassenger

Schedule

MeetingPoint

Address

RouteEvent

🛠 Tecnologias Utilizadas

Python 3.x

Django

Django REST Framework

PostgreSQL (desenvolvimento e produção; o único banco suportado)

Postman

🔌 Endpoints (Exemplo)
GET     /companies/
POST    /companies/

GET     /employees/
POST    /employees/

GET     /drivers/
POST    /drivers/

GET     /routes/
POST    /routes/

GET     /route-passengers/
POST    /route-passengers/

GET     /route-events/
POST    /route-events/
🧪 Estratégia de Validação

As regras de domínio são aplicadas por meio de:

validate_<field>()

validate()

Model.full_clean()

Serializers especializados (EmployeeSerializer / DriverSerializer)

Isso garante integridade forte sem poluir as views.