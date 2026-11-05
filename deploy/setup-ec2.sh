#!/usr/bin/env bash
# Prepara uma EC2 Ubuntu 24.04 recém-criada: Docker + swap + os dois repositórios.
# Rode como o usuário ubuntu:  bash setup-ec2.sh
set -euo pipefail

sudo apt-get update
sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"

# 2 GB de swap: o build do Angular passa do 1 GB de RAM de uma t3.micro/small.
if [ ! -f /swapfile ]; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

mkdir -p ~/rotacerta && cd ~/rotacerta
[ -d rota-certa-backend-django ] || git clone https://github.com/RaybrendoT/Rota-certa-Backend-Django.git rota-certa-backend-django
[ -d rota-certa-frontend-angular ] || git clone https://github.com/RaybrendoT/Rota-certa-Frontend-Angular.git rota-certa-frontend-angular

echo
echo "Pronto. Saia e entre de novo no SSH (para o grupo docker valer) e siga o deploy/README.md."
