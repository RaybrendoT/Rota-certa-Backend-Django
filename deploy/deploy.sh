#!/usr/bin/env bash
# Atualiza a stack na EC2. Roda sozinho pelo CI/CD (.github/workflows/ci-cd.yml) a cada push
# na main, ou à mão:
#   bash deploy/deploy.sh                  # imagem "latest" do backend
#   bash deploy/deploy.sh sha-1a2b3c4      # uma versão específica (rollback)
#   FRONTEND=1 bash deploy/deploy.sh       # também atualiza e reconstrói o Angular
set -euo pipefail

# Tudo dentro de main: o git pull abaixo pode trocar este arquivo enquanto ele roda.
main() {
  cd "$(dirname "$0")"

  export BACKEND_TAG="${1:-latest}"
  local frontend_dir="${FRONTEND_DIR:-../../rota-certa-frontend-angular}"

  # O Caddy só relê o Caddyfile ao reiniciar: guarda a versão antes do pull para comparar.
  local caddyfile_before
  caddyfile_before="$(sha256sum Caddyfile)"

  echo "==> Código do deploy (compose, Caddyfile, scripts)"
  git -C .. pull --ff-only
  mkdir -p secrets

  echo "==> Backend: ${BACKEND_IMAGE:-ghcr.io/raybrendot/rota-certa-backend-django}:${BACKEND_TAG}"
  docker compose pull backend

  # O Angular não tem imagem publicada: é construído aqui. Na primeira vez, sempre.
  if [ "${FRONTEND:-0}" = "1" ] || ! docker image inspect rotacerta-frontend:local > /dev/null 2>&1; then
    echo "==> Frontend (Angular): ${frontend_dir}"
    git -C "${frontend_dir}" pull --ff-only
    docker compose build frontend
  fi

  echo "==> Subindo"
  # Sem --build: o backend vem do GHCR. Ele aplica as migrações ao subir.
  docker compose up -d --no-build --remove-orphans

  if [ "$(sha256sum Caddyfile)" != "${caddyfile_before}" ]; then
    echo "==> Caddyfile mudou: reiniciando o Caddy"
    docker compose restart caddy
  fi

  # Guarda a versão no ar, para saber o que está rodando e para o rollback.
  echo "${BACKEND_TAG}" > .deployed-tag

  docker image prune -f > /dev/null
  docker compose ps
}

main "$@"
