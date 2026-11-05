"""API web de edgelab (FastAPI) + frontend estático en /."""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import COMPETITIONS, ROOT, settings
from ..core import db
from ..core import hedge as H
from ..core.markets import MARKETS, Selection, selection_probs
from ..models.inplay import InplayModel, InplayParams, LiveState
from ..models.poisson import implied_lambdas
from ..strategies.dsl import Strategy
from ..validation import stats

app = FastAPI(title="edgelab", version="1.0")
from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=2000)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Que el navegador no se quede con una versión vieja de la web al actualizar los archivos."""
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp
LIGHT = os.environ.get("EDGELAB_LIGHT") == "1"      # servidor pequeño (Render gratis: 512 MB)
_RC: dict = {}            # caché de respuestas: url → (hora, cuerpo, tipo)
_RC_SKIP = ("/api/live", "/api/status", "/api/health", "/api/login", "/api/debug", "/api/jobs", "/api/team/", "/api/alerts")
_RC_TTL = 90.0


@app.middleware("http")
async def response_cache(request: Request, call_next):
    """En servidores lentos (Render gratis, 0,1 CPU) cada página pedía lo mismo una y otra vez. Las respuestas de
    lectura se guardan 90 s: volver a una página ya vista es instantáneo. Cualquier cambio (POST) vacía la caché."""
    path = request.url.path
    if request.method != "GET" or not path.startswith("/api/") or path.startswith(_RC_SKIP) or os.environ.get("PYTEST_CURRENT_TEST"):
        if request.method in ("POST", "PUT", "DELETE"):
            _RC.clear()
        return await call_next(request)
    if settings.web_password and request.cookies.get("edgelab") != _token() and request.headers.get("x-edgelab") != _token():
        return await call_next(request)          # sin sesión: que responda la ruta (401), nunca la caché
    gz = "gzip" in (request.headers.get("accept-encoding") or "")
    key = (str(request.url), gz)
    hit = _RC.get(key)
    if hit and time.time() - hit[0] < _RC_TTL:
        return Response(hit[1], headers={**hit[2], "x-cache": "hit"})
    resp = await call_next(request)
    if resp.status_code != 200:
        return resp
    body = b"".join([c async for c in resp.body_iterator])
    hdrs = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-length",)}
    if len(_RC) > 400:
        _RC.clear()
    _RC[key] = (time.time(), body, hdrs)
    return Response(body, status_code=200, headers=hdrs)


WEB = ROOT / "web"
_jobs: dict[str, dict] = {}


# ------------------------------------------------------------------ auth sencilla
def _token() -> str:
    return hmac.new(settings.web_password.encode(), b"edgelab", hashlib.sha256).hexdigest()


def auth(request: Request) -> None:
    if not settings.web_password:
        return
    if request.cookies.get("edgelab") == _token() or request.headers.get("x-edgelab") == _token():
        return
    raise HTTPException(401, "login")


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login, response: Response):
    if settings.web_password and not hmac.compare_digest(body.password, settings.web_password):
        raise HTTPException(401, "contraseña incorrecta")
    response.set_cookie("edgelab", _token() if settings.web_password else "open", httponly=True, max_age=86400 * 60, samesite="lax")
    return {"ok": True}


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating,)):
        v = float(o)
        return None if v != v or v in (float("inf"), float("-inf")) else v
    if isinstance(o, float):
        return None if o != o or o in (float("inf"), float("-inf")) else o
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, np.ndarray):
        return _clean(o.tolist())
    return o


def J(o: Any) -> JSONResponse:
    return JSONResponse(_clean(o))


# ------------------------------------------------------------------ panel
@app.get("/api/health")
def health():
    """Sin datos privados: si la web y el planificador están vivos (para vigilarla desde fuera)."""
    now = int(time.time())
    def age(k):
        v = db.kv_get(k)
        return None if not v else now - int(v)
    doing = db.kv_get("worker_doing")
    return {"ok": True, "ts": now, "worker_s": age("worker_heartbeat"), "sync_s": age("last_sync"),
            "doing": (doing or {}).get("job") if isinstance(doing, dict) else None}


@app.get("/api/dashboard", dependencies=[Depends(auth)])
def dashboard():
    out: dict = {}
    for mode in ("paper", "real"):
        b = pd.DataFrame(db.q("SELECT stake, pnl, odds, clv, settled FROM bets WHERE mode=? AND status NOT IN ('open','void')", (mode,)))
        out[mode] = stats.summarize(b) if not b.empty else {"n": 0}
        if not b.empty:
            b = b.sort_values("settled")
            out[f"{mode}_curve"] = [{"t": int(t), "pnl": round(float(v), 2)} for t, v in zip(b["settled"], b["pnl"].cumsum())]
    out["open_bets"] = db.q1("SELECT COUNT(*) AS n FROM bets WHERE status='open'")["n"]
    out["strategies"] = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) AS n FROM strategies GROUP BY status")}
    out["data"] = {
        "matches": db.q1("SELECT COUNT(*) AS n FROM matches")["n"],
        "finished": db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='finished'")["n"],
        "with_timeline": db.q1("SELECT COUNT(DISTINCT match_id) AS n FROM timeline")["n"],
        "odds_rows": (db.q1("SELECT MAX(rowid) AS n FROM odds") or {}).get("n") or 0,
        "by_comp": db.q("SELECT comp, COUNT(*) AS n, MIN(season) AS first, MAX(season) AS last FROM matches GROUP BY comp"),
    }
    out["jobs"] = db.q("SELECT ts, job, status, msg FROM jobs_log ORDER BY id DESC LIMIT 15")
    out["lab"] = {k: v for k, v in (db.kv_get("last_lab_report") or {}).items() if k != "table"}
    out["alerts"] = db.q("SELECT id, ts, kind, title, message FROM alerts ORDER BY id DESC LIMIT 8")
    out["weekly"] = db.kv_get("weekly_report")
    return J(out)


# ------------------------------------------------------------------ partidos
@app.get("/api/matches", dependencies=[Depends(auth)])
def matches(days: int = 3, past: int = 0, comp: str | None = None):
    from ..config import settings as _st
    key = (str(_st.db_path), days, past, comp)
    hit = _MCACHE.get(key)
    if hit and time.time() - hit[0] < 45:
        return J(hit[1])
    res = _matches(days, past, comp)
    _MCACHE[key] = (time.time(), res)
    return J(res)


_MCACHE: dict = {}


def _matches(days: int, past: int, comp: str | None):
    now = int(time.time())
    sql = "SELECT * FROM matches WHERE kickoff BETWEEN ? AND ?"
    params: list = [now - past * 86400 - 3 * 3600, now + days * 86400]
    if comp:
        sql += " AND comp=?"
        params.append(comp)
    ms = db.q(sql + " ORDER BY kickoff LIMIT 400", params)
    preds = {r["match_id"]: json.loads(r["payload"]) for r in db.q(
        f"SELECT match_id, payload FROM predictions WHERE model='prematch' AND match_id IN ({','.join(str(m['id']) for m in ms) or '0'})")}
    picks = db.kv_get("prematch_picks", []) or []
    tids = {m["home_id"] for m in ms} | {m["away_id"] for m in ms}
    colors = {t["id"]: (t.get("color1"), t.get("color2")) for t in db.q(
        f"SELECT id, color1, color2 FROM teams WHERE id IN ({','.join(str(int(t)) for t in tids if t is not None) or '0'})")}
    form_cache: dict = {}
    def form(tid, before):
        k = (tid, before // 86400)
        if k not in form_cache:
            rows = db.q("SELECT * FROM (SELECT home_id, hg, ag, kickoff FROM matches WHERE home_id=? AND kickoff<? AND status='finished' AND hg IS NOT NULL "
                        "ORDER BY kickoff DESC LIMIT 5) UNION ALL SELECT * FROM (SELECT home_id, hg, ag, kickoff FROM matches WHERE away_id=? AND kickoff<? "
                        "AND status='finished' AND hg IS NOT NULL ORDER BY kickoff DESC LIMIT 5) ORDER BY kickoff DESC LIMIT 5", (tid, before, tid, before))
            form_cache[k] = "".join(("W" if (r["hg"] > r["ag"]) == (r["home_id"] == tid) and r["hg"] != r["ag"] else "D" if r["hg"] == r["ag"] else "L") for r in rows)[::-1]
        return form_cache[k]
    vals: dict = {}
    try:
        for v in _opps(72)["values"]:
            vals.setdefault(v["match"]["id"], []).append(v)
    except Exception:
        pass
    out = []
    for m in ms:
        p = preds.get(m["id"]) or {}
        orows = db.q("SELECT bookmaker, selection, odds FROM odds_now WHERE match_id=? AND market='1X2' AND period='FT' "
                     "AND bookmaker IN ('Bet365','Pinnacle','Sofascore')", (m["id"],))
        odds, book = {}, None
        for bk in ("Bet365", "Pinnacle", "Sofascore"):
            o = {}
            for r in orows:
                if r["bookmaker"] == bk:
                    o.setdefault(r["selection"], r["odds"])
            if len(o) == 3:
                odds, book = o, bk
                break
        out.append({**{k: m[k] for k in ("id", "comp", "kickoff", "home_name", "away_name", "status", "hg", "ag", "referee", "lineup_status")},
                    "model": {k: p.get(k) for k in ("pH", "pD", "pA", "lh", "la")}, "ens": p.get("ens"),
                    "b365": odds, "odds_book": book, "n_value": len(vals.get(m["id"], [])),
                    "best_value": (max(vals[m["id"]], key=lambda v: v["ev"])["label"] if vals.get(m["id"]) else None), "picks": [x for x in picks if x["match_id"] == m["id"]],
                    "home_id": m["home_id"], "away_id": m["away_id"],
                    "colors": {"H": colors.get(m["home_id"], (None, None)), "A": colors.get(m["away_id"], (None, None))},
                    "form": {"H": form(m["home_id"], m["kickoff"]), "A": form(m["away_id"], m["kickoff"])}})
    return out


@app.get("/api/match/{mid}", dependencies=[Depends(auth)])
def match_detail(mid: int):
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404)
    pred = db.q1("SELECT payload FROM predictions WHERE match_id=? AND model='prematch'", (mid,))
    pay = json.loads(pred["payload"]) if pred else None
    table: dict = {}      # las cuotas completas van por /markets (más rápido); aquí solo lo básico
    fair = None
    tl = db.q("SELECT minute, kind, side, xg, player FROM timeline WHERE match_id=? AND kind!='shot' ORDER BY minute", (mid,))
    bets = db.q("SELECT b.*, s.name AS strategy FROM bets b LEFT JOIN strategies s ON s.id=b.strategy_id WHERE match_id=?", (mid,))
    stats_rows = {r["side"]: json.loads(r["stats"]) for r in db.q("SELECT side, stats FROM match_stats WHERE match_id=?", (mid,))}
    tc = {t["id"]: t for t in db.q("SELECT id, color1, color2 FROM teams WHERE id IN (?,?)", (m["home_id"] or 0, m["away_id"] or 0))}
    m = {**m, "colors": {"H": [tc.get(m["home_id"], {}).get("color1"), tc.get(m["home_id"], {}).get("color2")],
                         "A": [tc.get(m["away_id"], {}).get("color1"), tc.get(m["away_id"], {}).get("color2")]}}
    lu = db.q("SELECT side, source, MAX(ts) AS ts, COUNT(*) AS n FROM lineups WHERE match_id=? AND source IN ('predicted','confirmed','played') "
              "GROUP BY side, source", (mid,))
    last_odds = db.kv_get(f"odds_checked:{mid}") or db.q1("SELECT MAX(ts) AS t FROM odds_now WHERE match_id=?", (mid,))["t"]
    n_odds = db.q1("SELECT COUNT(*) AS n FROM odds_now WHERE match_id=?", (mid,))["n"]
    has_b365 = bool(db.q1("SELECT 1 FROM odds_now WHERE match_id=? AND bookmaker='Bet365' LIMIT 1", (mid,)))
    live_rows = db.q("SELECT market, selection, line, period, odds, minute, score_h, score_a, ts FROM odds WHERE match_id=? AND phase='live' "
                     "AND ts>=(SELECT MAX(ts) FROM odds WHERE match_id=? AND phase='live')-5 ORDER BY market, line, selection", (mid, mid))
    fresh = {"odds_ts": last_odds, "n_odds": n_odds, "lineups": lu, "synced": db.kv_get(f"synced:{mid}"),
             "has_b365": has_b365, "timeline": len(tl)}
    need = (m["status"] == "notstarted" and (not n_odds or not lu or (last_odds or 0) < time.time() - 6 * 3600)) or \
           (m["status"] == "finished" and not tl) or m["status"] == "inprogress"
    fresh["needs_sync"] = bool(need) and (time.time() - (fresh["synced"] or 0) > (90 if m["status"] == "inprogress" else 900))
    return J({"match": m, "prediction": pay, "fair": fair, "odds": sorted(table.values(), key=lambda r: (r["market"], str(r["line"]), r["selection"])),
              "timeline": tl, "bets": bets, "stats": stats_rows, "freshness": fresh, "live_odds": live_rows,
              "live": next((x for x in (db.kv_get("live_snapshot", []) or []) if x and x.get("match_id") == mid), None)})


@app.get("/api/match/{mid}/markets", dependencies=[Depends(auth)])
def match_markets(mid: int, refresh: bool = False):
    """Todas las apuestas del partido con su precio, planes con coberturas, combinadas y surebets.
    Se sirve de la caché (como mucho 5 min de antigüedad, se refresca en segundo plano) para que abrir un partido sea instantáneo."""
    hit = _MK.get(mid)
    if hit and not refresh:
        if time.time() - hit[0] > 300 and mid not in _MK_BUSY:
            _MK_BUSY.add(mid)
            threading.Thread(target=_mk_bg, args=(mid,), daemon=True).start()
        return Response(hit[1], media_type="application/json")
    body = _markets_body(mid, refresh)
    if body is None:
        raise HTTPException(404)
    return Response(body, media_type="application/json")


def _mk_bg(mid: int) -> None:
    try:
        _markets_body(mid, False)
    except Exception:
        pass
    finally:
        _MK_BUSY.discard(mid)


def _markets_body(mid: int, refresh: bool) -> bytes | None:
    from ..plans import analyze, value_reason
    r = analyze(mid, use_cache=not refresh)
    if not r:
        return None
    r = {k: v for k, v in r.items() if k != "match"}
    slim = []
    for x in r.get("rows", []):
        if x.get("group") == "jugadores" and not x.get("b365"):
            continue          # sin cuota en bet365 no se puede apostar: fuera, que pesa mucho
        y = {k: v for k, v in x.items() if v is not None and k not in ("books",)}
        bks = x.get("books") or {}
        y["books"] = dict(list(bks.items())[:4 if x.get("group") == "jugadores" else 8])
        if x.get("ev") is not None and x["ev"] > 0:
            why = value_reason(x)
            y["rec"] = why is None            # la misma regla que la portada
            if why:
                y["why_not"] = why
        for k in ("p", "push", "p_model", "p_market", "ev", "ev_best"):
            if isinstance(y.get(k), float):
                y[k] = round(y[k], 4)
        slim.append(y)
    r["rows"] = slim
    body = J(r).body
    _MK.pop(mid, None)
    if len(_MK) >= 90:
        _MK.pop(next(iter(_MK)))
    _MK[mid] = (time.time(), body)
    return body


_MK_BUSY: set = set()


@app.get("/api/match/{mid}/bet", dependencies=[Depends(auth)])
def match_bet(mid: int, market: str, period: str = "FT", selection: str = "", line: str | None = None):
    """Análisis completo de una apuesta: historial, rival, duelo, árbitro, simulaciones, señales y texto."""
    from ..insights import bet_insight
    k = (mid, market, period, selection, line)
    hit = _BET.get(k)
    if hit and time.time() - hit[0] < 300:
        return Response(hit[1], media_type="application/json")
    from ..plans import vkey
    ln = None if line in (None, "", "null", "None") else float(line)
    pre = db.kv_get("betins:" + vkey(mid, market, period, selection, ln))     # precalculado por el planificador
    r = pre["r"] if pre and time.time() - pre.get("ts", 0) < 3600 else bet_insight(mid, market, period, selection, ln)
    if not r:
        raise HTTPException(404, "Esa apuesta ya no está en las cuotas de este partido")
    body = J(r).body
    if len(_BET) > 300:
        _BET.clear()
    _BET[k] = (time.time(), body)
    return Response(body, media_type="application/json")


_BET: dict = {}
_INS: dict = {}


@app.get("/api/match/{mid}/insights", dependencies=[Depends(auth)])
def match_insights(mid: int):
    """Cruces de estadísticas, duelos por banda, árbitro y 10.000 simulaciones del partido."""
    from ..insights import match_crosses, match_simulation
    hit = _INS.get(mid)
    if hit and time.time() - hit[0] < 600:
        return Response(hit[1], media_type="application/json")
    body = J({**match_crosses(mid), "sim": match_simulation(mid)}).body
    if len(_INS) > 80:
        _INS.clear()
    _INS[mid] = (time.time(), body)
    return Response(body, media_type="application/json")


@app.get("/api/player/{pid}/actions", dependencies=[Depends(auth)])
def player_actions(pid: int):
    """Pases, conducciones y acciones defensivas de sus últimos 3 partidos (se trae de Sofascore la primera vez)."""
    from ..collectors.tactics import player_actions as pa
    return J(pa(pid))
_MK: dict = {}


_OPP: dict = {}


_OPP_LOCK = threading.Lock()


def _opps(hours: float) -> dict:
    from ..plans import opportunities
    k = int(hours)
    with _OPP_LOCK:
        if _OPP.get(k, {}).get("t", 0) > time.time() - 120:
            return _OPP[k]["v"]
        v = opportunities(hours)
        _OPP[k] = {"t": time.time(), "v": v}
        return v


@app.get("/api/opportunities", dependencies=[Depends(auth)])
def opportunities_ep(hours: float = 72):
    return J(_opps(hours))


@app.get("/api/track", dependencies=[Depends(auth)])
def track(days: int = 90, mode: str = "tip"):
    """Historial de las recomendaciones oficiales (mode=tip) o de todo lo que detecta el escáner (mode=scan), 10 € planos."""
    from ..plans import track_record
    t = track_record(days, mode if mode in ("tip", "scan", "real", "opening") else "tip")
    if mode == "tip":
        sc = track_record(days, "scan")
        t["scan"] = {"all": sc["all"], "open": sc["open"], "by_group": sc["by_group"]}
        op = track_record(days, "opening")
        t["opening"] = {"all": op["all"], "open": op["open"], "by_group": op["by_group"]}
    return J(t)


_SYNC_LOCK = threading.Lock()


@app.post("/api/match/{mid}/refresh", dependencies=[Depends(auth)])
def match_refresh(mid: int):
    """Trae ahora cuotas, onces y cronología de este partido (Statshub + Sofascore)."""
    from ..live import engine
    _MK.pop(mid, None)
    with _SYNC_LOCK:
        return J(engine.sync_match(mid))


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    if _STATUS and time.time() - _STATUS[0] < 20:
        return J(_STATUS[1])
    v = _status()
    _STATUS[:] = [time.time(), v]
    return J(v)


_STATUS: list = []


def _status():
    now = int(time.time())
    hb = db.kv_get("worker_heartbeat") or 0
    up = db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now, now + 72 * 3600))
    ids = ",".join(str(r["id"]) for r in up) or "0"
    with_odds = db.q1(f"SELECT COUNT(DISTINCT match_id) AS n FROM odds_now WHERE match_id IN ({ids})")["n"]
    with_b365 = db.q1(f"SELECT COUNT(DISTINCT match_id) AS n FROM odds_now WHERE bookmaker='Bet365' AND match_id IN ({ids})")["n"]
    with_lu = db.q1(f"SELECT COUNT(DISTINCT match_id) AS n FROM lineups WHERE source IN ('predicted','confirmed') AND match_id IN ({ids})")["n"]
    with_pred = db.q1(f"SELECT COUNT(*) AS n FROM predictions WHERE model='prematch' AND match_id IN ({ids})")["n"]
    errs = db.q("SELECT ts, job, msg FROM jobs_log WHERE status='error' AND ts>? ORDER BY id DESC LIMIT 6", (now - 6 * 3600,))
    return ({"worker": {"alive": now - hb < 120, "heartbeat": hb, "doing": db.kv_get("worker_doing"),
                         "last_sync": db.kv_get("last_sync"), "live_heartbeat": db.kv_get("live_heartbeat")},
              "upcoming": {"n": len(up), "with_odds": with_odds, "with_b365": with_b365, "with_lineups": with_lu, "with_pred": with_pred},
              "live_now": db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='inprogress'")["n"],
              "timelines": db.q1("SELECT COUNT(DISTINCT match_id) AS n FROM timeline")["n"],
              "finished": db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='finished' AND id>0")["n"],
              "errors": [{**e, "msg": (e["msg"] or "")[:200]} for e in errs]})


@app.post("/api/jobs/sync", dependencies=[Depends(auth)])
def job_sync():
    hb = db.kv_get("worker_heartbeat") or 0
    if time.time() - hb < 120:
        db.kv_set("sync_request", int(time.time()))
        return {"ok": True, "via": "worker"}
    from ..worker import initial_sync
    def run(log):
        initial_sync(log=log)
        return {"ok": True}
    return _run_job("sync", run)


@app.get("/api/match/{mid}/report", dependencies=[Depends(auth)])
def match_report(mid: int, refresh: bool = False):
    from ..analysis import report
    if LIGHT:
        # servidor pequeño: el informe lo prepara el planificador (cargar el dataset aquí tumbaba la web: 502)
        c = db.q1("SELECT payload FROM reports WHERE match_id=?", (mid,))
        if not c:
            raise HTTPException(404, "El informe de este partido aún se está preparando")
        return J(json.loads(c["payload"]))
    r = report(mid, use_cache=not refresh)
    if not r:
        raise HTTPException(404)
    return J(r)


@app.get("/api/match/{mid}/odds_history", dependencies=[Depends(auth)])
def odds_history(mid: int, market: str = "1X2"):
    rows = db.q("SELECT ts, bookmaker, selection, line, odds FROM odds WHERE match_id=? AND market=? AND period='FT' AND phase!='live' "
                "AND bookmaker IN ('Bet365','Pinnacle','Betfair Exchange') ORDER BY ts", (mid, market))
    series: dict = {}
    for r in rows:
        k = f"{r['bookmaker']}|{r['selection']}|{'' if r['line'] is None else r['line']}"
        series.setdefault(k, []).append([r["ts"], r["odds"]])
    return J(series)


_BT_CACHE: dict = {}


def _bet_table():
    from ..backtest import prematch as bt_pre
    cache = settings.data_dir / "dataset.pkl"
    if not cache.exists():
        return None
    mt = cache.stat().st_mtime
    if _BT_CACHE.get("mt") != mt:
        _BT_CACHE.update({"mt": mt, "bt": bt_pre.bet_table(pd.read_pickle(cache))})
    return _BT_CACHE["bt"]


@app.get("/api/strategies/{sid}/curve", dependencies=[Depends(auth)])
def strategy_curve(sid: str):
    r = db.q1("SELECT spec FROM strategies WHERE id=?", (sid,))
    if not r:
        raise HTTPException(404)
    s = Strategy.from_dict(json.loads(r["spec"]))
    key = f"curve:{sid}"
    if key in _BT_CACHE:
        return J(_BT_CACHE[key])
    saved = db.kv_get(key)
    if saved:
        return J(saved)
    if LIGHT:      # recalcular la curva carga años de partidos en memoria: en el servidor pequeño, no
        return J({"points": [], "seasons": {}, "light": True})
    if s.phase == "pre":
        from ..backtest import prematch as bt_pre
        bt = _bet_table()
        bets = bt_pre.run(bt, s) if bt is not None else pd.DataFrame()
    else:
        from ..backtest import inplay as bt_live
        bets = bt_live.run(s, bt_live.load_timelines(), bt_live.PriceEngine(InplayParams.from_dict(db.load_params("inplay"))))
    rep = db.kv_get("last_lab_report") or {}
    oos = set(rep.get("oos_pre" if s.phase == "pre" else "oos_live") or [])
    out = {"points": [], "seasons": {}}
    if not bets.empty:
        bets = bets.sort_values("kickoff")
        cum = 0.0
        for b in bets.itertuples():
            cum += b.pnl
            out["points"].append([int(b.kickoff), round(cum, 3), 1 if b.season in oos else 0])
        for season, g in bets.groupby("season"):
            out["seasons"][str(season)] = {"n": int(len(g)), "yield": float(g["pnl"].sum() / g["stake"].sum()), "oos": season in oos}
        if len(out["points"]) > 600:
            step = len(out["points"]) // 600 + 1
            out["points"] = out["points"][::step] + [out["points"][-1]]
    _BT_CACHE[key] = out
    try:
        db.kv_set(key, out)      # queda guardada (también en la copia de la nube)
    except Exception:
        pass
    return J(out)


@app.get("/api/factors", dependencies=[Depends(auth)])
def factors_map():
    from ..strategies.cross import FACTORS
    return J({"grid": db.kv_get("factor_grid", []) or [], "names": FACTORS})


@app.get("/api/live/{mid}/props", dependencies=[Depends(auth)])
def live_props(mid: int):
    """Proyecciones en directo de equipos y jugadores (las calcula el planificador cada minuto)."""
    return J(db.kv_get(f"live_props:{mid}") or {})


@app.get("/api/live/{mid}/heatmap", dependencies=[Depends(auth)])
def live_heatmap(mid: int, pid: int | None = None, side: str | None = None):
    """Mapa de calor en directo (Sofascore): de un jugador (pid) o de un equipo (side=H|A). Rejilla 12×8 normalizada."""
    from ..collectors.sofascore import Sofascore, paused_left
    from ..collectors.tactics import grid_of
    ck = (mid, pid, side)
    hit = _HM.get(ck)
    if hit and time.time() - hit[0] < 300:
        return J(hit[1])
    if paused_left():
        return J({"grid": None, "error": "Sofascore nos ha frenado un rato; vuelve a probar en unos minutos"})
    sf = _SF.get("c") or _SF.setdefault("c", Sofascore())
    m = db.q1("SELECT home_id, away_id FROM matches WHERE id=?", (mid,))
    pts = []
    try:
        if pid:
            pts = sf.player_heatmap(mid, pid)
        elif side in ("H", "A") and m:
            d = sf.team_heatmap(mid, m["home_id"] if side == "H" else m["away_id"])
            pts = d.get("heatmap") or d.get("points") or [p for pl in (d.get("playerPoints") or []) for p in (pl.get("heatmap") or [])]
            # (antes, sin mapa de equipo se pedían los de 10 jugadores uno a uno: 10 peticiones que ayudaban a que
            # Sofascore nos bloqueara. Ahora una sola petición cada 5 min; los tiros y zonas salen de FotMob)
    except Exception as e:
        return J({"grid": None, "error": str(e)})
    res = {"grid": grid_of(pts) if pts else None, "n": len(pts)}
    _HM[ck] = (time.time(), res)
    return J(res)


_HM: dict = {}
_SF: dict = {}


@app.get("/api/match/{mid}/news", dependencies=[Depends(auth)])
def match_news(mid: int):
    """Titulares recientes de los dos equipos, pistas sacadas de ellos (posibles bajas, entrenador, rotaciones) y bajas oficiales."""
    from ..collectors.news import refresh
    r = refresh(mid)
    if not r:
        raise HTTPException(404)
    return J({"items": r.get("items", [])[:12], "flags": r.get("flags", []), "missing": (db.kv_get(f"missing:{mid}") or {}).get("players") or []})


@app.get("/api/match/{mid}/context", dependencies=[Depends(auth)])
def match_context(mid: int):
    """Cansancio, calendario y lo que se juega cada equipo."""
    from ..context import match_context as mc
    c = db.kv_get(f"context2:{mid}")
    if not c or time.time() - c.get("ts", 0) > 3600:
        c = {"ts": int(time.time()), "ctx": mc(mid)}
        db.kv_set(f"context2:{mid}", c)
    return J(c["ctx"])


@app.get("/api/review", dependencies=[Depends(auth)])
def review_get():
    return J(db.kv_get("review") or {})


@app.post("/api/review/run", dependencies=[Depends(auth)])
def review_run():
    from ..review import weekly_review
    threading.Thread(target=lambda: weekly_review(days=10, log=None), daemon=True).start()
    return {"ok": True}


@app.get("/api/debug/sofa", dependencies=[Depends(auth)])
def debug_sofa(path: str):
    """Consulta de diagnóstico a la API pública de Sofascore (solo lectura, solo rutas de Sofascore)."""
    from ..collectors.sofascore import Sofascore
    if not path.startswith("/") or ".." in path:
        raise HTTPException(400)
    return J(Sofascore()._get(path) or {})


@app.post("/api/results/run", dependencies=[Depends(auth)])
def results_run():
    """Cerrar ya los partidos acabados: estadística final, resolver apuestas y comparar con lo esperado."""
    from ..postmatch import finish_recent
    threading.Thread(target=lambda: finish_recent(36), daemon=True).start()
    return {"ok": True}


@app.get("/api/debug/finish/{mid}", dependencies=[Depends(auth)])
def debug_finish(mid: int):
    """Diagnóstico: intenta cerrar un partido ya y cuenta qué datos hay."""
    import traceback
    from ..postmatch import _from_statshub, compare
    m = db.q1("SELECT * FROM matches WHERE id=?", (mid,))
    out = {"status": m and m["status"]}
    try:
        from ..live.engine import shub
        g = shub().team_last_games(m["home_id"], 1, 3, ttl=0)
        out["shub_last_ids"] = [((x.get("event") or {}).get("id"), (x.get("event") or {}).get("timeStartTimestamp")) for x in g][:3]
        _from_statshub(m)
    except Exception:
        out["err_shub"] = traceback.format_exc()[-800:]
    out["player_stats"] = db.q1("SELECT COUNT(*) AS n FROM player_stats WHERE match_id=?", (mid,))["n"]
    out["match_stats"] = db.q1("SELECT COUNT(*) AS n FROM match_stats WHERE match_id=?", (mid,))["n"]
    try:
        rep = compare(mid)
        out["players"], out["teams"], out["markets"] = len(rep["players"]), len(rep["teams"]), len(rep["markets"])
    except Exception:
        out["err_cmp"] = traceback.format_exc()[-800:]
    return J(out)


@app.get("/api/debug/ladder/{mid}", dependencies=[Depends(auth)])
def debug_ladder(mid: int):
    """Diagnóstico de la escalera: rachas y cuotas de cada jugador en las líneas 1,5 y 2,5."""
    from ..plans import _ladder_legs, _streak, _PLAYER_KINDS
    d = _ladder_legs(mid)
    ko = int(d["match"]["kickoff"])
    by: dict = {}
    for x in d.get("legs", []):
        if x.get("pid") and x.get("kind") in _PLAYER_KINDS and str(x["dir"]).endswith(":O") and x.get("line") is not None:
            by.setdefault((x["pid"], x["kind"], x.get("player")), {})[float(x["line"])] = x["b365"]
    out = []
    for (pid, kind, name), ls in by.items():
        for Lm in (1.5, 2.5):
            if Lm in ls:
                st = _streak(pid, kind, Lm, ko)
                out.append({"p": name, "k": kind, "L": Lm, "o": ls[Lm], "low": ls.get(Lm - 1), "up": ls.get(Lm + 1),
                            "st": None if not st else round(st[0], 2), "n": None if not st else st[1]})
    out.sort(key=lambda r: -(r["st"] or 0))
    return J({"n_legs": len(d.get("legs", [])), "rows": out[:60]})


@app.get("/api/match/{mid}/postmatch", dependencies=[Depends(auth)])
def match_postmatch(mid: int):
    """Lo que esperábamos antes del partido frente a lo que pasó."""
    return J(db.kv_get(f"post:{mid}") or {})


@app.get("/api/bias", dependencies=[Depends(auth)])
def bias_get():
    from ..postmatch import bias_factor
    b = db.kv_get("bias") or {}
    return J({"team": {k: {**v, "f": bias_factor("team", k)} for k, v in (b.get("team") or {}).items()},
              "player": {k: {**v, "f": bias_factor("player", k)} for k, v in (b.get("player") or {}).items()},
              "minutes": b.get("minutes"), "n": len(b.get("matches") or []),
              "rows": {k: v for k, v in (db.kv_get("row_calib") or {}).items() if k != "matches"},
              "rows_n": len((db.kv_get("row_calib") or {}).get("matches") or [])})


@app.get("/api/alerts", dependencies=[Depends(auth)])
def alerts_list(kind: str | None = None, hours: float = 24, since: int | None = None, limit: int = 30):
    """Avisos guardados (p. ej. kind=mercado: mercados recién abiertos con valor)."""
    t0 = since if since is not None else int(time.time() - hours * 3600)
    sql = "SELECT id, ts, kind, title, message, match_id FROM alerts WHERE ts>?"
    params: list = [t0]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    return J(db.q(sql + " ORDER BY id DESC LIMIT ?", params + [max(1, min(limit, 100))]))


@app.get("/api/credits", dependencies=[Depends(auth)])
def credits(amount: float = 10.0, hours: float = 48):
    """Créditos de apuesta de bet365: dónde meterlos (una sola apuesta o repartidos)."""
    from ..plans import credit_plan
    return J(credit_plan(max(0.5, min(amount, 10000.0)), hours))


@app.get("/api/ladders", dependencies=[Depends(auth)])
def ladders_get():
    return J(db.kv_get("ladders") or {"items": []})


@app.get("/api/match/{mid}/ladder", dependencies=[Depends(auth)])
def match_ladder(mid: int):
    from ..plans import ladder
    return J(ladder(mid) or {})


@app.get("/api/live", dependencies=[Depends(auth)])
def live():
    from ..collectors.sofascore import paused_left
    return J({"snapshot": db.kv_get("live_snapshot", []) or [], "sofa_paused": paused_left(), "sources": db.kv_get("live_sources") or {},
              "alerts": db.q("SELECT * FROM alerts WHERE kind IN ('live','hedge') ORDER BY id DESC LIMIT 40")})


# ------------------------------------------------------------------ calculadoras
class HedgeLeg(BaseModel):
    market: str
    selection: str
    line: float | None = None
    odds: float
    stake: float = 0.0
    base_h: int = 0
    base_a: int = 0


class HedgeReq(BaseModel):
    positions: list[HedgeLeg]
    hedges: list[HedgeLeg]
    objective: str = "balance"
    zero_branch: str | None = None
    branches: list[str] | None = None
    bankroll: float | None = None
    # estado del partido para la rejilla de probabilidades
    minute: float = 0
    hg: int = 0
    ag: int = 0
    red_h: int = 0
    red_a: int = 0
    lh: float | None = None
    la: float | None = None
    odds_1x2: list[float] | None = None     # si no hay λ: se deducen de cuotas 1X2 del momento
    match_id: int | None = None


@app.post("/api/hedge", dependencies=[Depends(auth)])
def hedge(req: HedgeReq):
    model = InplayModel(InplayParams.from_dict(db.load_params("inplay")))
    st = LiveState(req.minute, req.hg, req.ag, req.red_h, req.red_a)
    lh, la = req.lh, req.la
    if (lh is None or la is None) and req.match_id:
        p = db.q1("SELECT payload FROM predictions WHERE match_id=? AND model='prematch'", (req.match_id,))
        if p:
            pay = json.loads(p["payload"])
            lh, la = pay["lh"], pay["la"]
    if lh is None or la is None:
        if req.odds_1x2 and len(req.odds_1x2) == 3:
            from ..core.markets import devig
            ph, pd_, pa = devig(req.odds_1x2)
            if req.minute <= 1:
                lh, la = implied_lambdas(ph, pd_, pa)
            else:
                # busca λ pre-partido que reproduzcan las cuotas del momento en este estado
                from scipy.optimize import minimize
                def loss(x):
                    P = model.final_distribution(float(np.exp(x[0])), float(np.exp(x[1])), st)
                    h, d, a = np.tril(P, -1).sum(), np.trace(P), np.triu(P, 1).sum()
                    return (h - ph) ** 2 + (d - pd_) ** 2 + (a - pa) ** 2
                r = minimize(loss, np.log([1.4, 1.1]), method="Nelder-Mead", options={"maxiter": 150})
                lh, la = float(np.exp(r.x[0])), float(np.exp(r.x[1]))
        else:
            lh, la = 1.45, 1.15
    P = model.final_distribution(lh, la, st)
    pos = [H.Position(Selection(l.market, l.selection, l.line, "FT", l.base_h, l.base_a), l.odds, l.stake) for l in req.positions]
    hed = [(Selection(l.market, l.selection, l.line, "FT", l.base_h, l.base_a), l.odds) for l in req.hedges]
    res = H.solve_hedge(pos, hed, P, objective=req.objective, zero_branch=req.zero_branch, bankroll=req.bankroll,
                        branches=req.branches)
    out = res.to_dict()
    out["lambdas"] = [lh, la]
    out["probs_1x2"] = [float(np.tril(P, -1).sum()), float(np.trace(P)), float(np.triu(P, 1).sum())]
    out["hedge_fair_odds"] = []
    for sel, _ in hed:
        w, p, l = selection_probs(sel, P)
        out["hedge_fair_odds"].append(round(1 + l / max(w, 1e-9), 3))
    return J(out)


class SimpleCalc(BaseModel):
    kind: str
    params: dict


@app.post("/api/calc", dependencies=[Depends(auth)])
def calc(req: SimpleCalc):
    p = req.params
    k = req.kind
    if k == "lay":
        return J(H.lay_hedge(p["stake"], p["odds"], p["lay_odds"], p.get("commission", 0.05)))
    if k == "zero":
        return J({"hedge_stake": H.hedge_to_zero(p["stake"], p["hedge_odds"])})
    if k == "equalize":
        return J({"hedge_stake": H.hedge_equalize(p["stake"], p["odds"], p["hedge_odds"])})
    if k == "dutch":
        return J(H.dutch(p["odds"], p.get("total_stake"), p.get("target_return")))
    if k == "greenup":
        return J(H.green_up(p["stake"], p["odds"], p["odds_now"]))
    if k == "kelly":
        return J({"stake": H.kelly_stake(p["prob"], p["odds"], p.get("bankroll", settings.bankroll), p.get("fraction", 0.25),
                                          p.get("push", 0.0), p.get("cap", settings.max_stake_pct))})
    if k == "fair":
        model = InplayModel(InplayParams.from_dict(db.load_params("inplay")))
        lh, la = p.get("lh"), p.get("la")
        if lh is None:
            from ..core.markets import devig
            lh, la = implied_lambdas(*devig(p["odds_1x2"]), p.get("p_over25"))
        fp = model.fair_prices(lh, la, LiveState(p.get("minute", 0), p.get("hg", 0), p.get("ag", 0), p.get("red_h", 0), p.get("red_a", 0),
                                                 p.get("xg_h"), p.get("xg_a"), p.get("added")))
        fp.pop("matrix", None)
        return J(fp)
    raise HTTPException(400, "calculadora desconocida")


# ------------------------------------------------------------------ estrategias
@app.get("/api/strategies", dependencies=[Depends(auth)])
def strategies(status: str | None = None, phase: str | None = None, limit: int = 300):
    sql = "SELECT id, name, family, origin, status, stats, spec, updated FROM strategies"
    params: list = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    rows = db.q(sql + " ORDER BY updated DESC LIMIT ?", params + [limit * 3])
    out = []
    for r in rows:
        spec = json.loads(r["spec"])
        if phase and spec.get("phase") != phase:
            continue
        st = json.loads(r["stats"]) if r["stats"] else {}
        paper = db.q1("SELECT COUNT(*) AS n, SUM(pnl) AS pnl, SUM(stake) AS stake FROM bets WHERE strategy_id=? AND status NOT IN ('open','void')", (r["id"],))
        out.append({"id": r["id"], "name": r["name"], "family": r["family"], "origin": r["origin"], "status": r["status"],
                    "phase": spec.get("phase"), "description": st.get("description") or Strategy.from_dict(spec).describe(),
                    "hypothesis": spec.get("hypothesis"), "data_needs": spec.get("data_needs"),
                    "is": st.get("in_sample"), "oos": st.get("oos"), "reasons": st.get("reasons"), "p_adj": st.get("p_adj"),
                    "paper": {"n": paper["n"], "pnl": paper["pnl"], "yield": (paper["pnl"] / paper["stake"]) if paper["stake"] else None}})
    rank = {"live": 0, "paper": 1, "validated": 2, "candidate": 3, "rejected": 4, "retired": 5}
    out.sort(key=lambda x: (rank.get(x["status"], 9), -((x.get("oos") or {}).get("shrunk") or -1)))
    return J(out[:limit])


@app.get("/api/strategies/{sid}", dependencies=[Depends(auth)])
def strategy_detail(sid: str):
    r = db.q1("SELECT * FROM strategies WHERE id=?", (sid,))
    if not r:
        raise HTTPException(404)
    tests = db.q("SELECT * FROM strategy_tests WHERE strategy_id=? ORDER BY ts DESC LIMIT 50", (sid,))
    bets = db.q("SELECT b.*, m.home_name, m.away_name FROM bets b JOIN matches m ON m.id=b.match_id WHERE strategy_id=? ORDER BY created DESC LIMIT 200", (sid,))
    spec = json.loads(r["spec"])
    return J({**r, "spec": spec, "stats": json.loads(r["stats"]) if r["stats"] else None, "tests": tests, "bets": bets,
              "description": Strategy.from_dict(spec).describe()})


class StatusBody(BaseModel):
    status: str


@app.post("/api/strategies/{sid}/status", dependencies=[Depends(auth)])
def set_status(sid: str, body: StatusBody):
    if body.status not in ("candidate", "validated", "paper", "live", "retired", "rejected"):
        raise HTTPException(400)
    db.execute("UPDATE strategies SET status=?, updated=? WHERE id=?", (body.status, int(time.time()), sid))
    return {"ok": True}


class BacktestBody(BaseModel):
    spec: dict


@app.post("/api/backtest", dependencies=[Depends(auth)])
def backtest(body: BacktestBody):
    """Backtest rápido de una estrategia escrita a mano (JSON de 7 piezas)."""
    if LIGHT:
        raise HTTPException(503, "Los backtests no caben en el servidor gratuito: hazlos en la versión del PC")
    s = Strategy.from_dict({"name": body.spec.get("name", "manual"), "phase": body.spec.get("phase", "pre"), **body.spec})
    s.origin = "manual"
    if s.phase == "pre":
        from ..backtest import prematch as bt_pre
        cache = settings.data_dir / "dataset.pkl"
        if not cache.exists():
            raise HTTPException(409, "Aún no hay dataset: ejecuta el laboratorio una vez")
        ds = pd.read_pickle(cache)
        bets = bt_pre.run(bt_pre.bet_table(ds), s)
    else:
        from ..backtest import inplay as bt_live
        bets = bt_live.run(s, bt_live.load_timelines(), bt_live.PriceEngine(InplayParams.from_dict(db.load_params("inplay"))))
    summ = stats.summarize(bets)
    from ..lab import save_strategy, record_test
    save_strategy(s, "candidate", {"in_sample": summ, "description": s.describe(), "manual": True})
    record_test(s.id, "manual", summ, "all")
    sample = bets.tail(50).to_dict("records") if not bets.empty else []
    return J({"id": s.id, "summary": summ, "description": s.describe(), "sample": sample})


# ------------------------------------------------------------------ apuestas
@app.get("/api/bets", dependencies=[Depends(auth)])
def bets(mode: str | None = None, status: str | None = None, limit: int = 300, resolved: int = 0):
    sql = ("SELECT b.*, m.home_name, m.away_name, m.kickoff, m.comp, s.name AS strategy FROM bets b JOIN matches m ON m.id=b.match_id "
           "LEFT JOIN strategies s ON s.id=b.strategy_id WHERE 1=1")
    params: list = []
    limit = max(1, min(limit, 1500))          # respuestas enormes tumbaban el servidor pequeño (502)
    if resolved:
        sql += " AND b.status NOT IN ('open','void')"
    if mode:
        sql += " AND b.mode=?"
        params.append(mode)
    if status:
        sql += " AND b.status=?"
        params.append(status)
    return J(db.q(sql + " ORDER BY b.created DESC LIMIT ?", params + [limit]))


class TakenBody(BaseModel):
    odds: float
    stake: float | None = None


@app.post("/api/bets/{bid}/taken", dependencies=[Depends(auth)])
def bet_taken(bid: int, body: TakenBody):
    """Marca que la apuesta la has hecho de verdad (con la cuota real que te dieron)."""
    b = db.q1("SELECT * FROM bets WHERE id=?", (bid,))
    if not b:
        raise HTTPException(404)
    db.execute("INSERT INTO bets(strategy_id,group_id,match_id,created,mode,market,selection,line,period,base_h,base_a,odds,odds_source,"
               "fair_prob,stake,minute,status,notes) SELECT strategy_id,group_id,match_id,?, 'real',market,selection,line,period,base_h,base_a,?,"
               "'user',fair_prob,?,minute,'open','copiada de papel' FROM bets WHERE id=?",
               (int(time.time()), body.odds, body.stake or b["stake"], bid))
    return {"ok": True}


class ManualBet(BaseModel):
    match_id: int
    market: str
    selection: str
    line: float | None = None
    period: str = "FT"
    odds: float
    stake: float
    label: str | None = None
    fair_prob: float | None = None


@app.post("/api/bets/manual", dependencies=[Depends(auth)])
def bet_manual(b: ManualBet):
    """«La he hecho» desde una recomendación: queda como apuesta real para medir tu rendimiento."""
    bid = db.execute("INSERT INTO bets(strategy_id,group_id,match_id,created,mode,market,selection,line,period,odds,odds_source,fair_prob,"
                     "edge,stake,status,notes) VALUES(NULL,NULL,?,?,'real',?,?,?,?,?,'user',?,?,?,'open',?)",
                     (b.match_id, int(time.time()), b.market, b.selection, b.line, b.period, b.odds, b.fair_prob,
                      (b.fair_prob * b.odds - 1) if b.fair_prob else None, b.stake, json.dumps({"label": b.label})))
    return {"ok": True, "id": bid}


class ResultBody(BaseModel):
    result: str          # won | lost | push | halfwon | halflost | void


@app.post("/api/bets/{bid}/result", dependencies=[Depends(auth)])
def bet_result(bid: int, body: ResultBody):
    """Liquidar a mano (mercados de jugadores y especiales, que no se pueden liquidar solos)."""
    b = db.q1("SELECT * FROM bets WHERE id=?", (bid,))
    if not b:
        raise HTTPException(404)
    f = {"won": (1, 0), "lost": (0, 0), "push": (0, 1), "void": (0, 1), "halfwon": (0.5, 0.5), "halflost": (0, 0.5)}.get(body.result)
    if f is None:
        raise HTTPException(400, "resultado no válido")
    w, p = f
    pnl = b["stake"] * (w * (b["odds"] - 1) - (1 - w - p))
    db.execute("UPDATE bets SET status=?, pnl=?, settled=? WHERE id=?", (body.result, round(pnl, 2), int(time.time()), bid))
    return {"ok": True, "pnl": round(pnl, 2)}


class ImportBody(BaseModel):
    csv: str


@app.post("/api/bets/import", dependencies=[Depends(auth)])
def import_bets(body: ImportBody):
    from ..learning.loop import import_real_bets
    return J(import_real_bets(body.csv))


# ------------------------------------------------------------------ aprendizaje y trabajos
_LEARN: dict = {}


@app.get("/api/learning", dependencies=[Depends(auth)])
def learning():
    from ..learning.loop import calibration_report
    if _LEARN.get("t", 0) > time.time() - 600:
        return J(_LEARN["v"])
    rep = db.kv_get("last_lab_report") or {}
    _LEARN["t"] = time.time()
    _LEARN["v"] = ({"calibration": calibration_report(), "inplay_params": db.load_params("inplay"),
              "inplay_info": db.load_params("inplay_info"), "model_scores": db.kv_get("model_scores"),
              "lab": rep, "tests_total": db.q1("SELECT COUNT(*) AS n FROM strategy_tests")["n"],
              "weekly": db.kv_get("weekly_report")})
    return J(_LEARN["v"])


def _run_job(name: str, fn, *a, **k):
    if _jobs.get(name, {}).get("running"):
        raise HTTPException(409, f"{name} ya está en marcha")
    logs: list[str] = []
    _jobs[name] = {"running": True, "started": int(time.time()), "log": logs}
    def target():
        try:
            res = fn(*a, log=lambda s: logs.append(f"{time.strftime('%H:%M:%S')} {s}"), **k)
            _jobs[name]["result"] = {k2: v for k2, v in (res or {}).items() if k2 != "table"} if isinstance(res, dict) else res
        except Exception as e:  # pragma: no cover
            logs.append(f"ERROR: {e}")
            db.log_job(name, "error", str(e))
        finally:
            _jobs[name]["running"] = False
            _jobs[name]["ended"] = int(time.time())
    threading.Thread(target=target, daemon=True).start()
    return {"ok": True}


@app.post("/api/jobs/lab", dependencies=[Depends(auth)])
def job_lab():
    # si el planificador está vivo lo hace él (otro proceso): la web no se queda sin CPU
    if time.time() - (db.kv_get("worker_heartbeat") or 0) < 120:
        db.kv_set("lab_request", int(time.time()))
        return {"ok": True, "via": "worker"}
    from ..lab import LabConfig, run_lab
    return _run_job("lab", run_lab, LabConfig())


@app.get("/api/jobs", dependencies=[Depends(auth)])
def jobs():
    return J({"jobs": {k: {kk: vv for kk, vv in v.items()} for k, v in _jobs.items()},
              "log": db.q("SELECT * FROM jobs_log ORDER BY id DESC LIMIT 60")})


DEFAULT_USER = {"bankroll": None, "risk": 1.0, "min_conf": "media", "show_players": True}


def user_settings() -> dict:
    u = {**DEFAULT_USER, **(db.kv_get("user_settings") or {})}
    if not u["bankroll"]:
        u["bankroll"] = settings.bankroll
    return u


class SettingsBody(BaseModel):
    bankroll: float | None = None
    risk: float | None = None
    min_conf: str | None = None
    show_players: bool | None = None


class TgBody(BaseModel):
    token: str | None = None
    group: bool = False          # True: mandar los avisos al grupo donde se ha añadido el bot


@app.get("/api/telegram", dependencies=[Depends(auth)])
def tg_status():
    from ..notify import tg_conf
    t, c = tg_conf()
    return {"token": bool(t), "chat": bool(c), "group": (db.kv_get("telegram") or {}).get("group"),
            "last_error": db.kv_get("tg_last_error")}


@app.post("/api/telegram", dependencies=[Depends(auth)])
def tg_setup(b: TgBody):
    """Guarda el token del bot y busca tu chat (tienes que haberle escrito antes cualquier cosa al bot)."""
    import httpx
    kv = db.kv_get("telegram") or {}
    token = (b.token or kv.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "Falta el token del bot")
    try:
        me = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
        if not me.get("ok"):
            raise HTTPException(400, "Telegram dice que el token no es válido")
        up = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"No se pudo hablar con Telegram: {e}")
    chats = [u["message"]["chat"]["id"] for u in up.get("result", [])
             if (u.get("message") or {}).get("chat", {}).get("type") == "private"]
    # grupos: el bot recibe un aviso cuando lo añaden (my_chat_member) y los /comandos que se le escriben allí
    groups = []
    for u in up.get("result", []):
        msg = (u.get("message") or u.get("my_chat_member") or u.get("channel_post") or {})
        c = msg.get("chat") or {}
        if c.get("type") in ("group", "supergroup", "channel"):
            groups.append((c["id"], c.get("title") or "grupo"))
        if msg.get("migrate_to_chat_id"):                  # el grupo pasó a supergrupo: vale el número nuevo
            groups.append((msg["migrate_to_chat_id"], c.get("title") or "grupo"))
    same_bot = kv.get("token") == token
    if b.group:
        if not groups:
            db.kv_set("telegram", {**kv, "token": token, "bot": me["result"].get("username")})
            return {"ok": False, "bot": me["result"].get("username"),
                    "msg": f"No veo ningún grupo. Añade @{me['result'].get('username')} al grupo, escribe en el grupo /start@{me['result'].get('username')} y vuelve a pulsar «Usar grupo»."}
        chat, title = str(groups[-1][0]), groups[-1][1]
        db.kv_set("telegram", {"token": token, "chat_id": chat, "bot": me["result"].get("username"), "group": title})
        from ..notify import send_telegram
        send_telegram(f"✅ edgelab conectado a «{title}»: aquí llegarán las apuestas del día y los avisos en directo.")
        return {"ok": True, "bot": me["result"].get("username"), "msg": f"Conectado al grupo «{title}». Te acabo de mandar un mensaje de prueba allí."}
    # el chat ya conectado NO se cambia aunque otra persona le escriba al bot (antes se cogía el último que escribiera)
    chat = kv.get("chat_id") if (same_bot and kv.get("chat_id") and not kv.get("group")) else (str(chats[-1]) if chats else None)
    db.kv_set("telegram", {"token": token, "chat_id": chat, "bot": me["result"].get("username")})
    if not chat:
        return {"ok": False, "bot": me["result"].get("username"), "msg": "Token guardado. Ahora abre tu bot en Telegram, escríbele «hola» y vuelve a pulsar Conectar."}
    from ..notify import send_telegram
    send_telegram("✅ edgelab conectado: aquí te llegarán las apuestas del día y los avisos en directo.")
    return {"ok": True, "bot": me["result"].get("username"), "msg": "Conectado. Te acabo de mandar un mensaje de prueba."}


@app.get("/api/settings", dependencies=[Depends(auth)])
def get_settings():
    return user_settings()


@app.post("/api/settings", dependencies=[Depends(auth)])
def set_settings(b: SettingsBody):
    u = user_settings()
    for k, v in b.model_dump().items():
        if v is not None:
            u[k] = v
    if u["bankroll"] <= 0 or not (0.1 <= u["risk"] <= 3):
        raise HTTPException(400, "valores no válidos")
    db.kv_set("user_settings", u)
    return u


@app.get("/api/meta")
def meta():
    u = user_settings()
    teams = {r["name"]: r["id"] for r in db.q("SELECT id, name FROM teams WHERE id>0")}
    return {"competitions": {k: c.name for k, c in COMPETITIONS.items()}, "markets": MARKETS,
            "auth": bool(settings.web_password), "bankroll": u["bankroll"], "user": u, "teams": teams}


LOGOS = Path(settings.data_dir) / "logos"


@app.get("/api/team/{tid}/logo")
def team_logo(tid: int):
    """Escudo oficial del equipo (Sofascore; guardado en disco la primera vez)."""
    LOGOS.mkdir(parents=True, exist_ok=True)
    f = LOGOS / f"{tid}.png"
    if not f.exists():
        miss = LOGOS / f"{tid}.none"
        if miss.exists() and time.time() - miss.stat().st_mtime < 86400:
            raise HTTPException(404)
        body = None
        try:
            from ..collectors.http import PoliteClient
            c = PoliteClient(min_interval=0.3)
            for url in (f"https://img.sofascore.com/api/v1/team/{tid}/image", f"https://api.sofascore.app/api/v1/team/{tid}/image"):
                try:
                    r = c._sess().get(url, timeout=10) if hasattr(c, "_sess") else None
                    if r is not None and r.status_code == 200 and (r.content[:4] == b"\x89PNG" or r.content[:4] == b"RIFF"):
                        body = r.content
                        break
                except Exception:
                    continue
        except Exception:
            body = None
        if not body:
            miss.write_text("")
            raise HTTPException(404)
        f.write_bytes(body)
    kind = "image/webp" if f.read_bytes()[:4] == b"RIFF" else "image/png"
    return FileResponse(str(f), media_type=kind, headers={"Cache-Control": "public, max-age=604800"})


# ------------------------------------------------------------------ web
if WEB.exists():
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/")
def index():
    return FileResponse(str(WEB / "index.html"))


def _warm_loop() -> None:
    """Precalienta los mercados de los partidos de las próximas 36 h para que abrir un partido sea instantáneo."""
    time.sleep(20)
    while True:
        try:
            now = int(time.time())
            for r in db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ? ORDER BY kickoff LIMIT 70",
                          (now, now + 72 * 3600)):
                try:
                    hit = _MK.get(r["id"])
                    if not hit or time.time() - hit[0] > 600:
                        _markets_body(r["id"], False)
                except Exception:
                    pass
                time.sleep(0.5)
        except Exception:
            pass
        time.sleep(300)


@app.on_event("startup")
def _start_warm() -> None:
    threading.Thread(target=_warm_loop, daemon=True, name="warm").start()
