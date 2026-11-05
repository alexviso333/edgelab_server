#!/usr/bin/env bash
# Instalación en una VM Debian/Ubuntu (p. ej. Google Cloud e2-micro gratuita, us-east1).
# Uso (en la VM):  curl -fsSL <raw de este fichero> | sudo bash   o   sudo bash deploy/install.sh
set -euo pipefail
REPO="${REPO:-https://github.com/alexviso333/edgelab.git}"
DIR=/opt/edgelab

echo "==> Paquetes"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git sqlite3

echo "==> Swap de 2 GB (la e2-micro solo tiene 1 GB de RAM)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Usuario y código"
id edgelab &>/dev/null || useradd -r -m -d /home/edgelab -s /bin/bash edgelab
if [ ! -d "$DIR/.git" ]; then
  if [ -d "$(dirname "$0")/../edgelab" ]; then cp -r "$(dirname "$0")/.." "$DIR"; else git clone "$REPO" "$DIR"; fi
fi
chown -R edgelab:edgelab "$DIR"

echo "==> Entorno Python"
sudo -u edgelab python3 -m venv "$DIR/.venv"
sudo -u edgelab "$DIR/.venv/bin/pip" install --upgrade pip
sudo -u edgelab "$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

if [ ! -f "$DIR/.env" ]; then
  cp "$DIR/.env.example" "$DIR/.env"
  PW=$(head -c 18 /dev/urandom | base64 | tr -d '/+=')
  sed -i "s/^EDGELAB_PASSWORD=.*/EDGELAB_PASSWORD=$PW/" "$DIR/.env"
  chown edgelab:edgelab "$DIR/.env"; chmod 600 "$DIR/.env"
  echo "Contraseña de la web: $PW   (guárdala; está en $DIR/.env)"
fi

echo "==> Base de datos y servicios"
sudo -u edgelab bash -c "cd $DIR && .venv/bin/python -m edgelab.cli init"
cp "$DIR/deploy/edgelab-api.service" "$DIR/deploy/edgelab-worker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now edgelab-api edgelab-worker

echo "==> Comprobación de fuentes desde esta IP"
sudo -u edgelab bash -c "cd $DIR && .venv/bin/python -m edgelab.cli check" || true

IP=$(curl -s -H 'Metadata-Flavor: Google' http://metadata/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip || hostname -I | awk '{print $1}')
cat <<MSG

Listo. Web: http://$IP:8000  (abre el puerto 8000 en el firewall de GCP, ver README)
Siguiente paso (tarda horas la primera vez, se puede dejar corriendo):
  sudo -u edgelab bash -c "cd $DIR && nohup .venv/bin/python -m edgelab.cli backfill --seasons 6 > data/backfill.log 2>&1 &"
Después:  sudo -u edgelab bash -c "cd $DIR && .venv/bin/python -m edgelab.cli lab"
Logs:     journalctl -u edgelab-worker -f
MSG
