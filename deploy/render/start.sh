#!/bin/sh
# Arranque en Render: recuperar datos de Hugging Face, planificador en segundo plano y web en $PORT.
cd /app
python -c "from edgelab import cloud; cloud.restore()" || true
python -m edgelab.cli init
python -u -m edgelab.cli worker &
exec python -u -c "
import os, threading, signal, sys
from edgelab import cloud
stop = threading.Event()
threading.Thread(target=cloud.loops, args=(stop,), daemon=True).start()
def bye(*a):
    stop.set(); cloud.backup(); sys.exit(0)
signal.signal(signal.SIGTERM, bye)
import uvicorn
uvicorn.run('edgelab.api.app:app', host='0.0.0.0', port=int(os.environ.get('PORT', '10000')), workers=1)
"
