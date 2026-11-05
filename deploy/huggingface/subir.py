"""Sube edgelab a Hugging Face Spaces (gratis, 2 CPU y 16 GB de RAM, encendido 24 h).

Uso (desde la carpeta de edgelab, con el .bat):  subir_a_huggingface.bat          → todo (código + base de datos)
                                                 subir_a_huggingface.bat codigo   → solo el código (actualizar)
Necesitas una cuenta gratis en huggingface.co y un token de escritura: huggingface.co/settings/tokens → New token → Write.
"""
from __future__ import annotations

import getpass
import gzip
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TOKF = ROOT / "data" / ".hf_token"


def main() -> None:
    from huggingface_hub import HfApi
    only_code = len(sys.argv) > 1 and sys.argv[1].lower().startswith("cod")
    tok = TOKF.read_text().strip() if TOKF.exists() else ""
    if not tok:
        print("Pega tu token de Hugging Face (de escritura). No se ve al escribir; pulsa Enter al acabar.")
        tok = getpass.getpass("Token: ").strip()
        TOKF.write_text(tok)
    api = HfApi(token=tok)
    user = api.whoami()["name"]
    space, data = f"{user}/edgelab", f"{user}/edgelab-data"
    print(f"Cuenta: {user}")

    api.create_repo(data, repo_type="dataset", private=True, exist_ok=True)
    if not only_code:
        tmp = Path(tempfile.mkdtemp())
        print("Copiando la base de datos (coherente aunque la web esté abierta)…")
        snap = tmp / "edgelab.sqlite"
        src = sqlite3.connect(str(ROOT / "data" / "edgelab.sqlite"), timeout=60)
        dst = sqlite3.connect(str(snap))
        src.backup(dst, pages=4096)
        dst.close()
        src.close()
        for path, name in ((snap, "edgelab.sqlite.gz"), (ROOT / "data" / "dataset.pkl", "dataset.pkl.gz")):
            if not path.exists():
                continue
            print(f"Comprimiendo {path.name}…")
            gz = tmp / name
            with open(path, "rb") as s, gzip.open(gz, "wb", compresslevel=5) as d:
                shutil.copyfileobj(s, d, 16 << 20)
            print(f"Subiendo {name} ({gz.stat().st_size >> 20} MB)… puede tardar unos minutos")
            api.upload_file(path_or_fileobj=str(gz), path_in_repo=name, repo_id=data, repo_type="dataset")
        shutil.rmtree(tmp, ignore_errors=True)

    api.create_repo(space, repo_type="space", space_sdk="docker", private=True, exist_ok=True)
    pw = ""
    try:
        have = {s.key for s in api.get_space_secrets(space)} if hasattr(api, "get_space_secrets") else set()
    except Exception:
        have = set()
    if "EDGELAB_PASSWORD" not in have:
        pw = getpass.getpass("Elige una contraseña para entrar a la web (no se ve al escribir): ").strip()
    api.add_space_secret(space, "HF_TOKEN", tok)
    api.add_space_secret(space, "DATA_REPO", data)
    if pw:
        api.add_space_secret(space, "EDGELAB_PASSWORD", pw)

    print("Subiendo el código…")
    stage = Path(tempfile.mkdtemp())
    for f in ("Dockerfile", "start.sh", "README.md"):
        shutil.copy(HERE / f, stage / f)
    shutil.copy(ROOT / "requirements.txt", stage / "requirements.txt")
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "edgelab", stage / "edgelab", ignore=ign)
    shutil.copytree(ROOT / "web", stage / "web", ignore=ign)
    api.upload_folder(folder_path=str(stage), repo_id=space, repo_type="space", commit_message="edgelab")
    shutil.rmtree(stage, ignore_errors=True)
    host = f"https://{user}-edgelab.hf.space"
    print("\nListo. El Space se está construyendo (5-10 minutos la primera vez).")
    print(f"Panel: https://huggingface.co/spaces/{space}")
    print(f"Tu web: {host}  (entra con tu cuenta de Hugging Face y la contraseña que elegiste)")
    print("Cuando funcione, cierra la versión del PC para que no haya dos planificadores.")


if __name__ == "__main__":
    main()
