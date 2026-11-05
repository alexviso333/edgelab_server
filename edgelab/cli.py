"""Línea de comandos.

  python -m edgelab.cli init                      crea la base de datos
  python -m edgelab.cli backfill --seasons 6      histórico gratis (Statshub + football-data + Understat)
  python -m edgelab.cli lab                       búsqueda + validación de estrategias
  python -m edgelab.cli app                       web + planificador juntos (lo que abre el .bat)
  python -m edgelab.cli api                       solo la web (puerto 8000)
  python -m edgelab.cli timelines --limit 500     cronologías (minuto de goles, rojas, xG por tiro) desde Sofascore
  python -m edgelab.cli sync                      calendario + cuotas + alineaciones de los próximos días ya
  python -m edgelab.cli worker                    planificador 24/7
  python -m edgelab.cli demo                      mundo sintético + laboratorio (sin red, para probar)
  python -m edgelab.cli report                    informe semanal ahora
  python -m edgelab.cli check                     comprueba que Statshub/fuentes responden desde esta máquina
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="edgelab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    b = sub.add_parser("backfill")
    b.add_argument("--seasons", type=int, default=6)
    b.add_argument("--skip-statshub", action="store_true")
    b.add_argument("--skip-understat", action="store_true")
    b.add_argument("--skip-fd", action="store_true")
    l = sub.add_parser("lab")
    l.add_argument("--quick", action="store_true")
    a = sub.add_parser("api")
    a.add_argument("--port", type=int, default=8000)
    a.add_argument("--host", default="0.0.0.0")
    sub.add_parser("worker")
    ap_ = sub.add_parser("app")
    ap_.add_argument("--port", type=int, default=8000)
    ap_.add_argument("--host", default="127.0.0.1")
    ap_.add_argument("--no-browser", action="store_true")
    tl = sub.add_parser("timelines")
    tl.add_argument("--limit", type=int, default=500)
    sub.add_parser("sync")
    d = sub.add_parser("demo")
    d.add_argument("--seasons", type=int, default=5)
    sub.add_parser("report")
    sub.add_parser("check")
    args = ap.parse_args(argv)

    if args.cmd == "init":
        from .core import db
        db.get_con()
        print("Base de datos lista:", db.settings.db_path)
    elif args.cmd == "backfill":
        backfill(args)
    elif args.cmd == "lab":
        from .lab import LabConfig, run_lab
        cfg = LabConfig(grid_per_seed=20, live_grid_per_seed=20, genetic_generations=2, genetic_size=16) if args.quick else LabConfig()
        rep = run_lab(cfg)
        _print_table(rep)
    elif args.cmd == "api":
        import uvicorn
        uvicorn.run("edgelab.api.app:app", host=args.host, port=args.port, workers=1)
    elif args.cmd == "app":
        run_app(args)
    elif args.cmd == "timelines":
        from .live import engine
        n = engine.backfill_timelines(limit=args.limit, log=print)
        print(f"{n} cronologías nuevas")
    elif args.cmd == "sync":
        from .worker import initial_sync
        initial_sync(log=print)
    elif args.cmd == "worker":
        from .worker import main as wmain
        wmain()
    elif args.cmd == "demo":
        import os
        from pathlib import Path
        from .config import settings
        path = str(settings.db_path)
        if os.path.exists(path):
            import sqlite3
            try:
                real = sqlite3.connect(path).execute("SELECT COUNT(*) FROM matches WHERE source!='synthetic'").fetchone()[0]
            except Exception:
                real = 0
            if real:
                print(f"{path} ya tiene datos reales; la demo no los borra. Usa otra ruta con EDGELAB_DB=data/demo.sqlite")
                return
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from scripts.synthetic import main as synth
        synth(args.seasons, path)
        from .lab import LabConfig, run_lab
        rep = run_lab(LabConfig(grid_per_seed=25, live_grid_per_seed=25, genetic_generations=3, genetic_size=20))
        _print_table(rep)
    elif args.cmd == "report":
        from .learning.loop import weekly_report
        print(weekly_report())
    elif args.cmd == "check":
        check()


def _print_table(rep: dict) -> None:
    rows = sorted(rep.get("table", []), key=lambda r: -(r.get("oos_shrunk") or -9))
    print(f"\n{'veredicto':10} {'fase':5} {'n_oos':>6} {'yield_oos':>9} {'contraído':>9} {'p_adj':>6}  estrategia")
    for r in rows[:40]:
        print(f"{r['verdict']:10} {r['phase']:5} {r.get('oos_n') or 0:6d} {(r.get('oos_yield') or 0):+9.1%} "
              f"{(r.get('oos_shrunk') or 0):+9.1%} {(1.0 if r.get('p_adj') is None else r['p_adj']):6.3f}  {r['name'][:70]}")


def run_app(args) -> None:
    """Arranca el planificador en un proceso aparte y la web en este. Al cerrar, para los dos.
    Todo queda también en data/arranque.log (web) y data/worker.log (planificador)."""
    import socket
    import subprocess
    import threading
    import traceback
    import urllib.request
    import webbrowser
    from .config import settings
    url = f"http://localhost:{args.port}"
    logf = open(settings.data_dir / "arranque.log", "a", encoding="utf-8", buffering=1)

    def say(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        print(line, flush=True)
        logf.write(line + "\n")

    # ¿ya hay un edgelab abierto en ese puerto?
    with socket.socket() as sck:
        sck.settimeout(1)
        busy = sck.connect_ex(("127.0.0.1", args.port)) == 0
    if busy:
        say(f"Ya hay algo escuchando en el puerto {args.port} (¿otra ventana de edgelab abierta?). Abro el navegador.")
        if not args.no_browser:
            webbrowser.open(url)
        return
    say("Cargando (la primera vez tarda 20-40 s: numpy, pandas, scikit-learn)...")
    try:
        from .core import db
        db.get_con()
    except Exception:
        say("No se pudo abrir la base de datos:\n" + traceback.format_exc())
        raise
    wlog = open(settings.data_dir / "worker.log", "a", encoding="utf-8")
    w = subprocess.Popen([sys.executable, "-u", "-m", "edgelab.cli", "worker"], stdout=wlog, stderr=subprocess.STDOUT)
    say(f"Planificador en marcha (pid {w.pid}): calendario, cuotas, alineaciones y directo se actualizan solos.")

    def open_when_ready() -> None:
        for _ in range(240):
            try:
                urllib.request.urlopen(url + "/api/health", timeout=2)
                say(f"Web lista en {url}  (cierra esta ventana para apagarlo todo)")
                if not args.no_browser:
                    webbrowser.open(url)
                return
            except Exception:
                time.sleep(0.5)
        say("La web tarda demasiado en responder; mira data/arranque.log")
    threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        import uvicorn
        uvicorn.run("edgelab.api.app:app", host=args.host, port=args.port, workers=1, log_level="warning")
    except BaseException as e:
        if not isinstance(e, KeyboardInterrupt):
            say("La web se ha parado con un error:\n" + traceback.format_exc())
        raise
    finally:
        w.terminate()
        try:
            w.wait(10)
        except Exception:
            w.kill()


def backfill(args) -> None:
    from .collectors import footballdata, teams, understat
    from .config import COMPETITIONS
    from .core import db
    from .live import engine
    t0 = time.time()
    if not args.skip_statshub:
        print("1/4 Statshub: partidos por día (todas las competiciones objetivo)…")
        n = engine.backfill_statshub(days=365 * args.seasons)
        print(f"   {n} partidos")
        print("   stats por partido de cada equipo (xG, córners, tarjetas)…")
        engine.backfill_team_stats(limit=60 * args.seasons)
        print("   alineaciones jugadas y stats por jugador (duelos, factor alineación)…")
        engine.backfill_players(limit=min(500, 60 * args.seasons))
    if not args.skip_fd:
        print("2/4 football-data.co.uk: cuotas históricas de apertura y cierre…")
        res = footballdata.backfill(args.seasons + 2)
        print("   ", res)
    if not args.skip_understat:
        print("3/4 Understat: cronología de goles y xG por tiro…")
        us = understat.Understat()
        y = datetime.now(timezone.utc).year
        y = y if datetime.now(timezone.utc).month >= 7 else y - 1
        for comp, c in COMPETITIONS.items():
            if not c.understat_code:
                continue
            for k in range(args.seasons):
                try:
                    n = understat.backfill(comp, y - k, us)
                    print(f"   {comp} {y - k}: {n}")
                except Exception as e:
                    print(f"   {comp} {y - k}: {e}")
    print("   Sofascore: cronologías de la última temporada (minuto de goles, rojas, xG por tiro)…")
    from .live import engine as _e
    print("   ", _e.backfill_timelines(limit=400 * args.seasons, log=print))
    print("4/4 Fusionando equipos provisionales…", teams.reconcile_provisional())
    db.log_job("backfill", "ok", f"{time.time() - t0:.0f}s")
    print(f"Listo en {time.time() - t0:.0f}s. Siguiente paso: python -m edgelab.cli lab")


def check() -> None:
    from .collectors.http import PoliteClient
    from .collectors.statshub import Statshub
    from datetime import datetime, timezone
    tests = []
    try:
        ev = Statshub().events_by_date(datetime.now(timezone.utc))
        tests.append(("Statshub by-date", f"OK ({len(ev)} partidos hoy)"))
    except Exception as e:
        tests.append(("Statshub by-date", f"FALLO: {e}"))
    c = PoliteClient(min_interval=0.5)
    for name, url in (("football-data", "https://www.football-data.co.uk/mmz4281/2526/SP1.csv"),
                      ("Understat", "https://understat.com/league/La_liga/2025"),
                      ("Sofascore", "https://www.sofascore.com/api/v1/sport/football/events/live"),
                      ("Open-Meteo", "https://api.open-meteo.com/v1/forecast?latitude=40.4&longitude=-3.7&hourly=precipitation")):
        try:
            t = c.get_text(url)
            tests.append((name, f"OK ({len(t)} bytes)"))
        except Exception as e:
            tests.append((name, f"FALLO: {e}"))
    for n, r in tests:
        print(f"{n:20} {r}")


if __name__ == "__main__":
    main()
