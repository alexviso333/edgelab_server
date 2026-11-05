#!/bin/sh
# Arranque en Hugging Face Spaces: restaurar datos, planificador en segundo plano, web en el puerto 7860.
set -e
cd /app
python -c "from edgelab import cloud; cloud.restore()" || true
python -m edgelab.cli init
python -u -m edgelab.cli worker &
WORKER=$!
python -u -c "
import threading, signal, sys
from edgelab import cloud
stop = threading.Event()
threading.Thread(target=cloud.loops, args=(stop,), daemon=True).start()
def bye(*a):
    stop.set(); cloud.backup(); sys.exit(0)
signal.signal(signal.SIGTERM, bye)
import uvicorn
uvicorn.run('edgelab.api.app:app', host='0.0.0.0', port=7860, workers=1)
"
kill $WORKER || true
