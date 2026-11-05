#!/usr/bin/env bash
# Abre edgelab en Mac/Linux. Uso: ./abrir.sh  (demo)   o   ./abrir.sh real  (datos reales)
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
if [ ! -f data/edgelab.sqlite ]; then
  if [ "$1" = "real" ]; then .venv/bin/python -m edgelab.cli backfill --seasons 4 && .venv/bin/python -m edgelab.cli lab
  else EDGELAB_DB=data/edgelab.sqlite .venv/bin/python -m edgelab.cli demo; fi
fi
.venv/bin/python -m edgelab.cli app --host 127.0.0.1 --port 8000
