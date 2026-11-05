"""Modo nube (Hugging Face Spaces): la base de datos vive en un repositorio privado de datos de Hugging Face.

- Al arrancar se descarga la última copia (edgelab.sqlite.gz) y se descomprime.
- Cada 6 horas (y al apagarse) se hace una copia coherente con la API de copias de SQLite, se comprime y se sube,
  borrando el historial viejo del repositorio para no gastar espacio.
- Cada 3 horas la propia web se visita a sí misma por su dirección pública: así el Space no se duerme nunca
  (en el plan gratis se duerme tras 48 h sin visitas).
Solo se activa si existen las variables HF_TOKEN y DATA_REPO (las pone el script de subida como secretos del Space).
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path

from .config import settings

TOKEN = os.environ.get("HF_TOKEN", "")
REPO = os.environ.get("DATA_REPO", "")
FILES = ("edgelab.sqlite.gz", "dataset.pkl.gz")


def enabled() -> bool:
    return bool(TOKEN and REPO)


def _log(msg: str) -> None:
    print(f"[nube] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def restore() -> bool:
    """Descarga y descomprime la base de datos y el dataset del modelo."""
    if not enabled():
        return False
    from huggingface_hub import hf_hub_download
    ok = False
    for name in FILES:
        target = settings.data_dir / name[:-3]
        try:
            p = hf_hub_download(REPO, name, repo_type="dataset", token=TOKEN, local_dir=str(settings.data_dir / "_dl"))
            with gzip.open(p, "rb") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 16 << 20)
            os.remove(p)
            _log(f"restaurado {target.name} ({target.stat().st_size >> 20} MB)")
            ok = ok or name.startswith("edgelab")
        except Exception as e:
            _log(f"no se pudo restaurar {name}: {e}")
    return ok


def backup() -> bool:
    if not enabled():
        return False
    from huggingface_hub import HfApi
    api = HfApi(token=TOKEN)
    tmp = settings.data_dir / "_bk"
    tmp.mkdir(exist_ok=True)
    try:
        snap = tmp / "edgelab.sqlite"
        src = sqlite3.connect(str(settings.db_path), timeout=60)
        dst = sqlite3.connect(str(snap))
        src.backup(dst, pages=4096)                       # copia coherente aunque el planificador esté escribiendo
        dst.close()
        src.close()
        out = []
        for path, name in ((snap, "edgelab.sqlite.gz"), (settings.data_dir / "dataset.pkl", "dataset.pkl.gz")):
            if not path.exists():
                continue
            gz = tmp / name
            with open(path, "rb") as s, gzip.open(gz, "wb", compresslevel=5) as d:
                shutil.copyfileobj(s, d, 16 << 20)
            out.append((gz, name))
        for gz, name in out:
            api.upload_file(path_or_fileobj=str(gz), path_in_repo=name, repo_id=REPO, repo_type="dataset",
                            commit_message=f"copia {time.strftime('%Y-%m-%d %H:%M')}")
        try:
            api.super_squash_history(repo_id=REPO, repo_type="dataset")   # sin historial: no se acumulan copias viejas
        except Exception:
            pass
        _log("copia subida")
        return True
    except Exception as e:
        _log(f"copia fallida: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _keepalive() -> None:
    # Hugging Face (SPACE_HOST) o Render (RENDER_EXTERNAL_URL): visitarse a sí misma para que no la duerman
    url = os.environ.get("RENDER_EXTERNAL_URL") or (f"https://{os.environ['SPACE_HOST']}" if os.environ.get("SPACE_HOST") else "")
    if not url:
        return
    req = urllib.request.Request(f"{url.rstrip('/')}/api/health", headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        _log(f"auto-visita fallida: {e}")


def loops(stop: threading.Event) -> None:
    last_bk = time.time()
    last_ka = 0.0
    while not stop.wait(60):
        # Render duerme a los 15 min sin visitas; Hugging Face a las 48 h
        if time.time() - last_ka > (600 if os.environ.get("RENDER_EXTERNAL_URL") else 3 * 3600):
            last_ka = time.time()
            _keepalive()
        if time.time() - last_bk > 6 * 3600:
            last_bk = time.time()
            backup()
