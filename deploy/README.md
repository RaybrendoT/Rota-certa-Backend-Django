# Deploy na AWS (uma EC2 com Docker) e CI/CD

Tudo roda numa EC2 só, com `docker compose`:

| Container | O que é |
|---|---|
| `caddy` | HTTPS automático (Let's Encrypt) e roteamento entre backend e frontend |
| `backend` | Django em ASGI (daphne): API, admin, `/static/` e WebSocket. Aplica as migrações ao subir. |
| `frontend` | Angular (nginx), construído do repositório [Rota-certa-Frontend-Angular](https://github.com/RaybrendoT/Rota-certa-Frontend-Angular) |
| `db` | Postgres 16 (volume `postgres_data`) |
| `redis` | channel layer do WebSocket (Channels) |

Um domínio só atende tudo:

- `https://SEU_DOMINIO/` → Angular
- `https://SEU_DOMINIO/api/` → API (é essa URL que vai no app Android)
- `wss://SEU_DOMINIO/ws/stream/?token=...` → WebSocket
- `https://SEU_DOMINIO/admin/` → admin do Django

Com o CI/CD configurado (seção 6), cada push na `main` que passa no lint e nos testes vira
uma imagem nova no GHCR e vai para a EC2 sozinho.

## 1. Criar a EC2 (console da AWS)

1. **EC2 → Launch instance**
   - AMI: **Ubuntu Server 24.04 LTS**
   - Tipo: **t3.small** (2 GB de RAM, cerca de US$ 15/mês).
   - Key pair: crie uma e guarde o `.pem`.
   - Storage: **20 GB gp3**.
   - Security group: **22 (SSH)**, **80** e **443** liberadas (veja a seção 6.1 sobre a 22).
2. **EC2 → Elastic IPs → Allocate** e associe o IP à instância, para ele não mudar quando a máquina reiniciar.
3. No DNS do seu domínio, crie um **registro A** `SEU_DOMINIO → IP elástico`.
   Espere propagar antes do passo 3: o Caddy só consegue emitir o certificado com o DNS apontando certo.

## 2. Preparar o servidor

```bash
ssh -i sua-chave.pem ubuntu@IP_ELASTICO
curl -fsSL https://raw.githubusercontent.com/RaybrendoT/Rota-certa-Backend-Django/main/deploy/setup-ec2.sh -o setup-ec2.sh
bash setup-ec2.sh
exit   # entre de novo para o grupo docker valer
```

O script instala o Docker, cria 2 GB de swap e clona o backend e o Angular em `~/rotacerta/`.

## 3. Configurar e subir

```bash
cd ~/rotacerta/rota-certa-backend-django/deploy
cp .env.example .env
nano .env        # DOMAIN, SECRET_KEY, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, senhas, chaves
bash deploy.sh
docker compose logs -f backend caddy
```

- Gere a `SECRET_KEY` com `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`.
- Na `POSTGRES_PASSWORD`, use só letras e números: ela entra na `DATABASE_URL` que o compose monta.
- O superusuário do admin é criado na primeira subida com `DJANGO_SUPERUSER_*` do `.env`.

Na primeira vez, o `deploy.sh` constrói o Angular. Antes do primeiro pipeline rodar ainda não
existe imagem do backend no GHCR; nesse caso construa aqui mesmo:

```bash
docker compose build backend && bash deploy.sh
```

Conferência: `curl https://SEU_DOMINIO/api/health/` responde `{"status": "ok", "database": true}`.

## 4. Atualizar à mão

```bash
cd ~/rotacerta/rota-certa-backend-django
bash deploy/deploy.sh                  # backend na última imagem (latest)
FRONTEND=1 bash deploy/deploy.sh       # também atualiza e reconstrói o Angular
```

O `deploy.sh` faz `git pull` (compose, Caddyfile), baixa a imagem do backend, sobe tudo e limpa
imagens velhas. O Angular não tem imagem publicada: ele é construído na própria EC2.

### APK do app para baixar

A pasta `deploy/downloads/` é servida em `https://SEU_DOMINIO/download/`. Os arquivos dela não
vão para o git nem para os containers, então continuam lá depois dos deploys. Para publicar ou
trocar a APK (o QR de instalação aponta para `/download/rotacerta.apk`), na sua máquina:

```bash
scp -i RotaCerta.pem app-prod-debug.apk ubuntu@SEU_IP:~/rotacerta/rota-certa-backend-django/deploy/downloads/rotacerta.apk
```

O link é público: qualquer pessoa com ele baixa a APK (entrar no app continua exigindo login).

## 5. Backup do banco

```bash
crontab -e
# adicione:
0 3 * * * bash ~/rotacerta/rota-certa-backend-django/deploy/backup.sh
```

O dump vai para `~/backups/` (guarda 14 dias). Ele fica no mesmo disco da EC2, então copie
para um S3 de vez em quando, ou ative os snapshots do volume EBS (EC2 → Lifecycle Manager).

## 6. CI/CD (deploy automático)

O workflow `.github/workflows/ci-cd.yml` faz **lint → testes → build → push no GHCR → deploy**:

| Job | O que faz | Quando |
|---|---|---|
| **Lint** | `ruff check` (regras em `ruff.toml`) | sempre |
| **Testes** | `check`, `makemigrations --check`, `manage.py test` e o seed, num Postgres 16 do próprio runner | sempre |
| **Build e push** | imagem do `Dockerfile` em `ghcr.io/raybrendot/rota-certa-backend-django`, tags `sha-<commit>` e `latest` | sempre; em PR só constrói |
| **Deploy** | SSH na EC2, `deploy/deploy.sh <tag>` e conferência do `/api/health/` | push na `main` (ou manual), com `EC2_HOST` configurado |

Cada job só roda se o anterior passou. Em pull request o deploy aparece como *skipped*: é o
esperado. Lint local: `pip install ruff==0.17.3 && ruff check .` (`--fix` arruma os imports).

### 6.1. Chave SSH só para o deploy

Na sua máquina (não reaproveite o `.pem` da AWS):

```bash
ssh-keygen -t ed25519 -f rotacerta-deploy -N "" -C "github-actions"
ssh-copy-id -i rotacerta-deploy.pub -o "IdentityFile=sua-chave.pem" ubuntu@IP_ELASTICO
ssh-keyscan -H IP_ELASTICO      # guarde a saída para EC2_SSH_KNOWN_HOSTS
```

Sem `ssh-copy-id` (Windows): cole o conteúdo de `rotacerta-deploy.pub` numa linha nova de
`~/.ssh/authorized_keys` na EC2.

Os runners do GitHub não têm IP fixo, então a porta 22 precisa aceitar conexões de fora.
Com login só por chave (o padrão da AWS) isso é aceitável para um projeto deste tamanho.

### 6.2. Secrets e variáveis no GitHub

No repositório: **Settings → Secrets and variables → Actions**.

| Tipo | Nome | Valor |
|---|---|---|
| Secret | `EC2_SSH_KEY` | conteúdo do arquivo `rotacerta-deploy` (a chave **privada**) |
| Secret | `EC2_SSH_KNOWN_HOSTS` | saída do `ssh-keyscan` (opcional, mas evita aceitar um servidor falso) |
| Variable | `EC2_HOST` | IP elástico ou domínio da EC2. **Sem ela o deploy não roda.** |
| Variable | `DEPLOY_DOMAIN` | `SEU_DOMINIO`, para conferir `https://SEU_DOMINIO/api/health/` no fim |
| Variable | `EC2_USER` | opcional, padrão `ubuntu` |
| Variable | `DEPLOY_PATH` | opcional, padrão `~/rotacerta/rota-certa-backend-django` |

O job usa o ambiente `production`. Em **Settings → Environments → production** dá para exigir
aprovação manual antes de cada deploy.

### 6.3. Imagem no GHCR

A imagem aparece em **Packages** no GitHub. Ela não tem segredos, já que o `.env` fica fora
dela. O deploy faz login no GHCR com o token temporário do próprio workflow e sai no fim. Para
baixar a imagem à mão na EC2 (rollback fora do pipeline), deixe o pacote público
(**Package settings → Change visibility**) ou faça `docker login ghcr.io` com um token pessoal
que tenha `read:packages`.

### 6.4. Rollback

Cada commit da `main` tem a imagem `sha-<7 primeiros caracteres do commit>`. Na EC2:

```bash
cat ~/rotacerta/rota-certa-backend-django/deploy/.deployed-tag    # versão no ar
cd ~/rotacerta/rota-certa-backend-django && bash deploy/deploy.sh sha-1a2b3c4
```

Ou no GitHub: **Actions → CI/CD →** a execução do commit bom → **Re-run all jobs**.

O rollback volta o código, mas não desfaz migrações. Se a versão nova mudou o banco de um
jeito incompatível, restaure o backup (seção 5).
