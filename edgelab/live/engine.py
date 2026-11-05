"""Motor en tiempo real.

- Calendario: partidos de hoy y próximos 7 días (Statshub by-date).
- Grabador de cuotas pre-partido: odds-comparison de Statshub (bet365, Pinnacle,
  Betfair y 12 casas más, ~130 mercados) cada vez más a menudo según se acerca
  el saque. Esto construye tu propio histórico de apertura/cierre y CLV.
- Picks pre-partido: aplica las estrategias activas a los próximos partidos.
- Directo: cada minuto, marcador/estado (Statshub) + incidencias y xG
  (Sofascore si está accesible); evalúa las estrategias en vivo y avisa con la
  CUOTA MÍNIMA a la que merece la pena entrar y el stake; si luego toca cubrir,
  avisa con el stake de cobertura calculado.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from ..collectors import statshub as sh
from ..collectors import sofascore as sf
from ..collectors.sofascore import Sofascore, parse_incidents, parse_live_stats
from ..config import COMPETITIONS, settings
from ..core import db
from ..core.hedge import hedge_equalize, hedge_to_zero, kelly_stake
from ..core.markets import Selection, selection_probs
from ..models.inplay import InplayModel, InplayParams, LiveState
from ..notify import alert
from ..strategies.dsl import Strategy

_TL = threading.local()   # un cliente por hilo (las sesiones HTTP no se comparten entre hilos)


def shub() -> sh.Statshub:
    c = getattr(_TL, "sh", None)
    if c is None:
        c = _TL.sh = sh.Statshub()
    return c


SOFA_EVERY = 240     # segundos entre consultas a Sofascore por partido en directo (antes ~90: con 9 partidos a la vez nos bloqueaba)


def sofa() -> Sofascore:
    c = getattr(_TL, "sofa", None)
    if c is None:
        c = _TL.sofa = Sofascore()
    return c


# ------------------------------------------------------------------ calendario
def refresh_schedule(days_back: int = 1, days_ahead: int = 7) -> int:
    n = 0
    today = datetime.now(timezone.utc)
    for d in range(-days_back, days_ahead + 1):
        day = today + timedelta(days=d)
        try:
            ev = shub().events_by_date(day)
            n += len(sh.store_events(ev))
        except Exception as e:
            db.log_job("schedule", "error", f"{day:%Y-%m-%d}: {e}")
    db.log_job("schedule", "ok", f"{n} partidos")
    return n


def backfill_statshub(days: int, start: datetime | None = None, log=print) -> int:
    """Histórico por días (1 petición por día cubre todas las competiciones)."""
    start = start or datetime.now(timezone.utc)
    done = set(db.kv_get("backfill_days", []))
    n = 0
    for k in range(days):
        day = start - timedelta(days=k)
        key = day.strftime("%Y-%m-%d")
        if key in done:
            continue
        try:
            n += len(sh.store_events(shub().events_by_date(day, ttl=86400 * 365)))
            done.add(key)
            if k % 20 == 0:
                db.kv_set("backfill_days", sorted(done))
                log(f"  statshub {key}: {n} partidos acumulados")
        except Exception as e:
            log(f"  statshub {key}: {e}")
    db.kv_set("backfill_days", sorted(done))
    return n


def backfill_team_stats(limit: int = 60, log=print) -> int:
    """Stats por partido (xG, córners, tarjetas...) de todos los equipos de las competiciones objetivo."""
    teams = db.q("SELECT DISTINCT home_id AS id FROM matches WHERE home_id>0 UNION SELECT DISTINCT away_id FROM matches WHERE away_id>0")
    tids = [c.unique_id for c in COMPETITIONS.values()]
    n = 0
    for t in teams:
        try:
            tour = shub().team_tournaments(t["id"])
            ids = [k for k in tour.keys() if k.isdigit()] if isinstance(tour, dict) else []
            rows = shub().team_performance(t["id"], ids or tids, limit=limit)
            n += sh.store_team_stats(sh.parse_team_performance(rows, t["id"]))
        except Exception as e:
            log(f"  equipo {t['id']}: {e}")
    return n


# ------------------------------------------------------------------ cuotas pre-partido
def record_prematch_odds(hours_ahead: float = 168, force: bool = False) -> int:
    now = int(time.time())
    ms = db.q("SELECT id, home_name, away_name, kickoff FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?",
              (now, now + int(hours_ahead * 3600)))
    n = 0
    for m in ms:
        left_h = (m["kickoff"] - now) / 3600
        last_t = db.kv_get(f"odds_checked:{m['id']}") or 0
        # los mercados abren 5–7 días antes (o más): también los lejanos se miran a menudo para pillar la apertura
        # en las últimas 24 h, cada 10 min (5 min en las 3 últimas): así se ve a tiempo cuando se mueven las casas afiladas
        every = 3 if left_h > 72 else (2 if left_h > 24 else (1 / 6 if left_h > 3 else 1 / 12))
        if not force and now - last_t < every * 3600:
            continue
        try:
            data = shub().odds_comparison(m["id"], m["home_name"], m["away_name"])
            db.kv_set(f"book_upd:{m['id']}", data.get("bookmakerUpdatedAt") or {})
            rows = sh.parse_odds_comparison(data, m["home_name"], m["away_name"])
            phase = "open" if not last_t else ("close" if left_h <= 0.25 else "snap")
            n += sh.store_odds_rows(m["id"], rows, phase)
            if left_h <= 24:
                try:
                    from ..steam import steam_alerts
                    steam_alerts(m["id"])        # ¿Pinnacle/Betfair se han movido y bet365 no?
                except Exception as e:
                    db.log_job("odds", "error", f"movimientos {m['id']}: {e}")
        except Exception as e:
            db.log_job("odds", "error", f"{m['id']}: {e}")
    if n:
        db.log_job("odds", "ok", f"{n} cuotas")
    return n


def latest_odds(match_id: int, bookmaker: str = "Bet365", live: bool = False) -> dict:
    rows = db.q("SELECT market, selection, line, period, odds, ts FROM odds WHERE match_id=? AND bookmaker=? "
                + ("AND phase='live' " if live else "AND phase!='live' ") + "ORDER BY ts", (match_id, bookmaker))
    out = {}
    for r in rows:
        out[(r["market"], r["selection"], r["line"], r["period"])] = r["odds"]
    return out


# ------------------------------------------------------------------ Sofascore: cuotas, alineaciones, cronologías
def sofascore_prematch_odds(hours_ahead: float = 72, only_missing: bool = True, limit: int = 20) -> int:
    """Cuotas pre-partido de Sofascore para los partidos sin cuotas de Statshub (respaldo)."""
    now = int(time.time())
    if db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='inprogress'")["n"]:
        return 0        # con partidos en juego, Sofascore se reserva para el directo
    ms = db.q("SELECT id, home_name, away_name FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ? "
              "ORDER BY kickoff LIMIT ?", (now, now + int(hours_ahead * 3600), limit * 3))
    n = k = 0
    for m in ms:
        if k >= limit:
            break
        if only_missing and db.q1("SELECT 1 FROM odds_now WHERE match_id=? LIMIT 1", (m["id"],)):
            # con cuotas de Statshub solo hace falta si su bet365 está viejo o no está (Sofascore = cuotas de bet365 al día)
            from ..pricing import stale_books
            has365 = db.q1("SELECT 1 FROM odds_now WHERE match_id=? AND bookmaker='Bet365' LIMIT 1", (m["id"],))
            if has365 and "Bet365" not in stale_books(m["id"]):
                continue
            if now - int(db.kv_get(f"sofa_odds_ts:{m['id']}") or 0) < 3 * 3600:
                continue
        if not sofa().ok:
            break
        k += 1
        rows = sf.parse_odds(sofa().odds(m["id"]), m["home_name"], m["away_name"])
        if rows:
            db.kv_set(f"sofa_odds_ts:{m['id']}", now)
        n += sf.store_odds(m["id"], [r for r in rows if not r["live"]])
    if n:
        db.log_job("odds_sofa", "ok", f"{n} cuotas")
    return n


def fetch_timeline(match_id: int) -> int:
    incs = sofa().incidents(match_id, ttl=86400 * 30)
    if not incs:
        return 0
    shots = sofa().shotmap(match_id, ttl=86400 * 30)
    return sf.store_timeline(match_id, sf.build_timeline(incs, shots))


def backfill_timelines(limit: int = 150, log=None) -> int:
    """Cronología (goles con minuto, rojas, cambios, xG por tiro) de los partidos terminados que no la tienen.
    Reanudable: empieza por los más recientes y salta los ya hechos o sin datos."""
    tried = set(db.kv_get("tl_tried", []))
    comps = list(COMPETITIONS.keys())
    ph = ",".join("?" * len(comps))
    ms = db.q(f"SELECT id FROM matches m WHERE status='finished' AND id>0 AND comp IN ({ph}) "
              "AND NOT EXISTS (SELECT 1 FROM timeline t WHERE t.match_id=m.id) ORDER BY kickoff DESC LIMIT ?",
              (*comps, limit + len(tried)))
    n = done = 0
    for m in ms:
        if done >= limit or not sofa().ok:
            break
        if m["id"] in tried:
            continue
        done += 1
        try:
            k = fetch_timeline(m["id"])
        except Exception:
            k = 0
        if k:
            n += 1
        else:
            tried.add(m["id"])
        if log and done % 25 == 0:
            log(f"  cronologías: {done} revisados, {n} guardados")
    db.kv_set("tl_tried", sorted(tried)[-20000:])
    if done:
        db.log_job("timelines", "ok", f"{n}/{done} partidos")
    return n


def sync_match(match_id: int) -> dict:
    """Trae ya todo lo que falte de un partido (lo usa el botón/auto-refresco de la ficha)."""
    from ..collectors import players as pl
    m = db.q1("SELECT * FROM matches WHERE id=?", (match_id,))
    if not m:
        return {"ok": False, "error": "partido no encontrado"}
    out = {"ok": True, "odds": 0, "odds_sofa": 0, "lineups": 0, "timeline": 0, "errors": []}
    if m["status"] == "notstarted":
        try:
            data = shub().odds_comparison(m["id"], m["home_name"], m["away_name"])
            db.kv_set(f"book_upd:{match_id}", data.get("bookmakerUpdatedAt") or {})
            rows = sh.parse_odds_comparison(data, m["home_name"], m["away_name"])
            have = db.q1("SELECT 1 FROM odds_now WHERE match_id=? AND bookmaker!='Sofascore' LIMIT 1", (match_id,))
            out["odds"] = sh.store_odds_rows(match_id, rows, "snap" if have else "open")
        except Exception as e:
            out["errors"].append(f"cuotas Statshub: {e}")
        try:
            data = shub().predicted_lineup(match_id)
            try:
                forms = shub().formations(match_id)
            except Exception:
                forms = {}
            out["lineups"] = pl.store_predicted(match_id, data, forms)
        except Exception as e:
            out["errors"].append(f"once previsto Statshub: {e}")
    try:
        rows = sf.parse_odds(sofa().odds(match_id), m["home_name"], m["away_name"])
        live = m["status"] == "inprogress"
        out["odds_sofa"] = sf.store_odds(match_id, rows if live else [r for r in rows if not r["live"]],
                                         minute=sh.live_minute(m) if live else None,
                                         score=(m["hg"] or 0, m["ag"] or 0) if live else None)
    except Exception as e:
        out["errors"].append(f"cuotas Sofascore: {e}")
    try:
        lu = sf.parse_lineups(sofa().lineups(match_id))
        out["lineups"] += sf.store_lineups(match_id, lu)
    except Exception as e:
        out["errors"].append(f"alineación Sofascore: {e}")
    if m["status"] in ("finished", "inprogress"):
        try:
            out["timeline"] = fetch_timeline(match_id)
        except Exception as e:
            out["errors"].append(f"cronología: {e}")
    db.execute("DELETE FROM reports WHERE match_id=?", (match_id,))
    db.kv_set(f"synced:{match_id}", int(time.time()))
    return out


def live_price(match_id: int, sel: Selection, max_age: int = 240) -> float | None:
    """Última cuota en directo de bet365 (Oddspedia o Sofascore) de esa selección, si es reciente y del marcador actual."""
    now = int(time.time())
    rows = db.q("SELECT line, odds, score_h, score_a FROM odds WHERE match_id=? AND phase='live' AND market=? AND selection=? "
                "AND period=? AND ts>=? AND bookmaker IN ('Bet365','Sofascore') ORDER BY ts DESC LIMIT 20",
                (match_id, sel.market, sel.selection, sel.period or "FT", now - max_age))
    for r in rows:
        if sel.market in ("AH",) and (r["score_h"], r["score_a"]) != (sel.base_h, sel.base_a):
            continue
        if sel.line is None or r["line"] is None or abs(float(r["line"]) - float(sel.line)) < 1e-6:
            return r["odds"]
    return None


# ------------------------------------------------------------------ estrategias activas
def active_strategies(phase: str) -> list[Strategy]:
    rows = db.q("SELECT spec, status FROM strategies WHERE status IN ('paper','live','validated')")
    out = []
    for r in rows:
        try:
            s = Strategy.from_dict(json.loads(r["spec"]))
            if s.phase == phase:
                s.description = r["status"]
                out.append(s)
        except Exception:
            pass
    return out


def watch_strategies() -> list[Strategy]:
    """Estrategias en vivo que se evalúan en cada partido.

    - Las activas (papel / real / validadas) generan alerta al móvil.
    - Las semillas del catálogo se vigilan SIEMPRE en silencio (estado "watch"):
      así desde el primer día se acumula registro real de todas, también de las
      que aún no se pueden validar con datos históricos.
    """
    act = active_strategies("live")
    ids = {s.id for s in act}
    from ..strategies.catalog import catalog
    for s in catalog():
        if s.phase == "live" and not s.control and s.id not in ids:
            s.description = "watch"
            act.append(s)
    return act


def prematch_picks(hours_ahead: float = 48) -> list[dict]:
    """Aplica estrategias pre-partido activas a los próximos partidos con la última cuota de bet365."""
    import pandas as pd
    from ..backtest import prematch as bt_pre
    from ..models import features
    now = int(time.time())
    up = db.q("SELECT id FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?", (now, now + int(hours_ahead * 3600)))
    if not up:
        return []
    import pickle
    from ..config import settings as _s
    cache = _s.data_dir / "dataset.pkl"
    if cache.exists() and time.time() - cache.stat().st_mtime < 36 * 3600:
        ds_feat = pd.read_pickle(cache)
        # añade partidos nuevos que no estuvieran en la caché
        if not set(u["id"] for u in up) <= set(ds_feat["id"]):
            ds_feat = features.build()
    else:
        ds_feat = features.build()
    preds = {r["match_id"]: json.loads(r["payload"]) for r in db.q("SELECT match_id, payload FROM predictions WHERE model='prematch'")}
    ids = {u["id"] for u in up}
    fut = ds_feat[ds_feat["id"].isin(ids)].copy()
    for c, k in (("ens_open_pH", "ens_open_pH"), ("ens_open_pD", "ens_open_pD"), ("ens_open_pA", "ens_open_pA"),
                 ("ens_open_lh", "ens_open_lh"), ("ens_open_la", "ens_open_la"), ("ens_open_pO25", "ens_open_pO25")):
        fut[c] = [((preds.get(i) or {}).get("ens") or {}).get(k) for i in fut["id"]]
    # para próximos partidos la "apertura" es la última cuota disponible
    for i, r in fut.iterrows():
        lo = latest_odds(int(r["id"]), "Bet365")
        for s in "HDA":
            fut.at[i, f"o_b365_open_{s}"] = lo.get(("1X2", s, None, "FT"))
        fut.at[i, "o_b365_open_O25"] = lo.get(("OU", "O", 2.5, "FT"))
        fut.at[i, "o_b365_open_U25"] = lo.get(("OU", "U", 2.5, "FT"))
    bt = bt_pre.bet_table(fut, include_future=True)
    picks = []
    for s in active_strategies("pre"):
        bets = bt_pre.run(bt, s, bankroll=settings.bankroll)
        for _, b in bets.iterrows():
            m = db.q1("SELECT home_name, away_name, kickoff, comp FROM matches WHERE id=?", (int(b["id"]),))
            sel = Selection(b["market"], b["selection"], b["line"] if pd.notna(b["line"]) else None)
            p_used = b.get("p_used")
            stake = kelly_stake(p_used, b["odds"], settings.bankroll, settings.kelly_fraction, cap_pct=settings.max_stake_pct) \
                if p_used and pd.notna(p_used) else settings.bankroll * 0.01
            min_odds = round((1 + (s.model_filter.get("min_edge") or 0)) / p_used, 2) if p_used and pd.notna(p_used) else None
            pick = {"match_id": int(b["id"]), "strategy": s.name, "strategy_id": s.id, "status": s.description,
                    "selection": sel.label(m["home_name"], m["away_name"]), "odds": float(b["odds"]),
                    "min_odds": min_odds, "edge": float(b["edge"]) if pd.notna(b["edge"]) else None,
                    "stake": round(stake, 2), "kickoff": m["kickoff"], "match": f"{m['home_name']} - {m['away_name']}"}
            picks.append(pick)
            _paper_bet(s, int(b["id"]), sel, float(b["odds"]), stake, p_used, None)
            alert("prematch", f"PICK {m['home_name']} - {m['away_name']}",
                  f"{s.name}\n{pick['selection']} @ {pick['odds']} (mín. {min_odds})\nStake sugerido: {pick['stake']} €",
                  match_id=int(b["id"]), strategy_id=s.id, payload=pick, dedupe=f"pre:{s.id}:{b['id']}:{b['selection']}")
    db.kv_set("prematch_picks", picks)
    return picks


def _paper_bet(s: Strategy, match_id: int, sel: Selection, odds: float, stake: float, p: float | None,
               minute: float | None, group: str | None = None, mode: str = "paper") -> None:
    ex = db.q1("SELECT id FROM bets WHERE strategy_id=? AND match_id=? AND market=? AND selection=? AND mode IN ('paper','watch') "
               "AND COALESCE(group_id,'')=COALESCE(?, '')", (s.id, match_id, sel.market, sel.selection, group))
    if ex:
        return
    db.execute("INSERT INTO bets(strategy_id,group_id,match_id,created,mode,market,selection,line,period,base_h,base_a,odds,odds_source,"
               "fair_prob,edge,stake,minute,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
               (s.id, group, match_id, int(time.time()), mode, sel.market, sel.selection, sel.line, sel.period,
                sel.base_h, sel.base_a, odds, "bet365_snapshot" if minute is None else "model_live", p,
                (p * odds - 1) if p else None, round(stake, 2), minute))


# ------------------------------------------------------------------ directo
class LiveTracker:
    def __init__(self):
        self.model = InplayModel(InplayParams.from_dict(db.load_params("inplay")))
        self.state: dict[int, dict] = {}
        self.last_sofa: dict[int, float] = {}
        self.last_odds: dict[int, float] = {}
        self.last_bydate = 0.0
        self.sofa_every = float(SOFA_EVERY)

    def live_matches(self) -> list[dict]:
        now = int(time.time())
        return db.q("SELECT * FROM matches WHERE (status='inprogress') OR (status='notstarted' AND kickoff BETWEEN ? AND ?)",
                    (now - 3 * 3600, now + 60))

    def tick(self) -> list[dict]:
        ms = self.live_matches()
        if not ms:
            if db.kv_get("live_snapshot"):
                db.kv_set("live_snapshot", [])     # sin partidos en juego: que no se quede colgado el último minuto
            return []
        now = time.time()
        if now - self.last_bydate > 150:
            try:
                sh.store_events(shub().events_by_date(datetime.now(timezone.utc)))
                self.last_bydate = now
                ms = self.live_matches()
            except Exception as e:
                db.log_job("live", "error", f"by-date: {e}")
        try:
            scores = shub().live_scores([m["id"] for m in ms])
        except Exception as e:
            db.log_job("live", "error", f"live-scores: {e}")
            scores = {}
        out = []
        # ritmo de Sofascore según cuántos partidos hay a la vez (≈4 peticiones por partido y vuelta, máx. ~12/min):
        # con 1-4 partidos cada ~100 s; con 9, cada ~200 s; tras un bloqueo, más despacio
        slow = 2.5 if time.time() < sf._G.get("slow_until", 0) else 1.0
        self.sofa_every = min(SOFA_EVERY * slow, max(100.0, 22.0 * len(ms)) * slow)
        for m in ms:
            sc = scores.get(m["id"])
            st = self.state.setdefault(m["id"], {"events": [], "xg": (None, None), "reds": (0, 0)})
            if sc:
                hg, ag = int(sc.get("home") or 0), int(sc.get("away") or 0)
                status_txt = str(sc.get("status") or "")
                prev = st.get("score")
                if prev and (hg, ag) != prev:
                    # gol detectado por cambio de marcador
                    minute = sh.live_minute({**m, "extra": m.get("extra")}) or 0
                    side = "H" if hg > prev[0] else "A"
                    st["events"].append({"minute": minute, "kind": "goal", "side": side})
                st["score"] = (hg, ag)
                st["status_txt"] = status_txt
                db.execute("UPDATE matches SET hg=?, ag=?, status=? WHERE id=?",
                           (hg, ag, "finished" if status_txt.lower() in ("ended", "ft", "finished") else "inprogress", m["id"]))
            ss = self.sofa_state(m, st, now)
            if ss and ss["type"] in ("postponed", "canceled"):
                db.execute("UPDATE matches SET status=?, hg=NULL, ag=NULL WHERE id=?", (ss["type"], m["id"]))
                void_match_bets(m["id"], ss["type"])
                continue
            if ss and ss["type"] == "finished" and now - m["kickoff"] < 95 * 60:
                ss = None          # «terminado» antes de 95' desde el inicio: no puede ser, no se cierra nada
            if ss and ss["type"] == "finished":
                db.execute("UPDATE matches SET status='finished', hg=?, ag=? WHERE id=?", (ss["hg"], ss["ag"], m["id"]))
                continue
            if ss and ss["type"] == "inprogress":
                if ss["hg"] is not None:
                    st["score"] = (ss["hg"], ss["ag"])
                    db.execute("UPDATE matches SET status='inprogress', hg=?, ag=? WHERE id=?", (ss["hg"], ss["ag"], m["id"]))
                minute = ss["minute"]
            else:
                if now - m["kickoff"] > 130 * 60:
                    continue        # sin Sofascore y con más de 2 h desde el inicio: casi seguro acabado (se cierra en «resultados»)
                minute = self.fallback_minute(m, st, now)     # reloj + estado (el inicio de periodo de Statshub suele venir viejo)
                if minute is None:
                    minute = sh.live_minute(m)
            if minute is None:
                continue
            # Sofascore: rojas, xG por tiro, añadido (cada ~90 s) y cuotas en directo (cada ~45 s)
            # con FotMob al día, Sofascore solo cada 10 min para incidencias/xG (sus cuotas en directo siguen a su ritmo)
            if sofa().ok and now - self.last_sofa.get(m["id"], 0) > (max(600.0, self.sofa_every) if self.fm_fresh(st, now) else self.sofa_every):
                self.last_sofa[m["id"]] = now
                raw = sofa().incidents(m["id"])
                if raw:
                    st["incs_raw"] = raw
                incs = [e for e in parse_incidents(raw) if e["kind"] in ("goal", "red", "sub_def", "sub_att", "added")]
                if raw:
                    st["events"] = [e for e in incs if e["kind"] != "added"]
                    st["reds"] = (sum(1 for e in incs if e["kind"] == "red" and e["side"] == "H"),
                                  sum(1 for e in incs if e["kind"] == "red" and e["side"] == "A"))
                    st["added"] = next((float(e["player"]) for e in reversed(incs) if e["kind"] == "added" and e["minute"] >= 90 and e["player"] not in (None, "None")), None)
                    gh = sum(1 for e in incs if e["kind"] == "goal" and e["side"] == "H")
                    ga = sum(1 for e in incs if e["kind"] == "goal" and e["side"] == "A")
                    if "score" not in st:
                        st["score"] = (gh, ga)
                ls = parse_live_stats(sofa().statistics(m["id"]))
                if ls["H"].get("xg") is not None:
                    st["xg"] = (ls["H"]["xg"], ls["A"].get("xg"))
                else:
                    shots = sf.parse_shotmap(sofa().shotmap(m["id"]))
                    if shots:
                        st["xg"] = (round(sum(x["xg"] for x in shots if x["side"] == "H"), 2),
                                    round(sum(x["xg"] for x in shots if x["side"] == "A"), 2))
                st["stats"] = ls
                st["stats_sofa"], st["xg_sofa"], st["sofa_t"] = ls, st.get("xg"), now   # de ESPN se suma lo ocurrido después
            self.espn_live(m, st, now)
            # cuotas en directo: Oddspedia (bet365 y 10 casas más, sin bloqueos) cada 90 s; Sofascore solo si falla
            if now - st.get("odp_ts", 0) > 90:
                st["odp_ts"] = now
                try:
                    from ..collectors import oddspedia
                    rows = oddspedia.live_odds(m)
                    if rows:
                        oddspedia.store_live(m["id"], rows, minute, st.get("score"))
                        st["odp_ok"] = now
                        st["has_live_odds"] = True
                except Exception as e:
                    db.log_job("live", "error", f"Oddspedia {m['id']}: {e}")
            if now - st.get("odp_ok", 0) > 300 and sofa().ok and now - self.last_odds.get(m["id"], 0) > self.sofa_every + 60:
                self.last_odds[m["id"]] = now
                try:
                    rows = [r for r in sf.parse_odds(sofa().odds(m["id"]), m["home_name"], m["away_name"]) if r["live"]]
                    if rows:
                        sf.store_odds(m["id"], rows, minute=minute, score=st.get("score"))
                        st["has_live_odds"] = True
                except Exception as e:
                    db.log_job("live", "error", f"cuotas en vivo {m['id']}: {e}")
            if now - st.get("shub_odds_ts", 0) > 120:
                st["shub_odds_ts"] = now
                try:
                    self.statshub_live_odds(m)
                except Exception as e:
                    db.log_job("live", "error", f"cuotas en vivo Statshub {m['id']}: {e}")
            ev = self.evaluate(m, minute, st)
            try:
                lp = self.props(m, minute, st, now, (ev or {}).get("fair"))
                if lp and ev is not None:
                    ev["props"] = lp
            except Exception as e:
                db.log_job("live", "error", f"proyecciones {m['id']}: {e}")
            out.append(ev)
        db.kv_set("live_snapshot", [o for o in out if o])
        # estado de cada fuente (para que se vea en la web si va todo bien)
        sts = [self.state.get(m["id"]) or {} for m in ms]
        db.kv_set("live_sources", {"ts": int(now), "n": len(ms),
                                   "fotmob": int(max([x.get("fm_ok", 0) for x in sts] or [0])),
                                   "espn": int(max([max(x.get("esum_ok", 0), x.get("es_ts", 0)) for x in sts] or [0])),
                                   "s365": int(max([x.get("net_ok", 0) for x in sts] or [0])),
                                   "oddspedia": int(max([x.get("odp_ok", 0) for x in sts] or [0])),
                                   "sofa": int(max([self.last_sofa.get(m["id"], 0) for m in ms] or [0]))})
        return out

    def fm_fresh(self, st: dict, now: float) -> bool:
        return now - st.get("fm_ok", 0) < 120

    def espn_live(self, m: dict, st: dict, now: float) -> None:
        """Entre consultas a Sofascore: FotMob primero (Opta, ≈25 s de retraso, xG por tiro y stats de cada jugador)
        y, si no, ESPN (1-2 min tarde: solo se suma lo ocurrido después de la última consulta a Sofascore)."""
        if now - st.get("fm_ts", 0) >= 40:
            st["fm_ts"] = now
            try:
                from ..collectors import fotmob
                fm = fotmob.live_match(m)
            except Exception as e:
                fm = None
                db.log_job("live", "error", f"FotMob {m['id']}: {e}")
            if fm and fm.get("started") and fm is not st.get("fm"):
                st["fm"], st["fm_ok"] = fm, now
        if self.fm_fresh(st, now) and st.get("fm"):
            from ..collectors import fotmob
            fm = st["fm"]
            st["stats"] = fotmob.merge_team(st.get("stats_sofa"), fm)
            if fm["xg"][0] is not None:
                st["xg"] = fm["xg"]                 # xG de Opta, al día
            if fm.get("mom"):
                st["mom"] = fm["mom"]
            return
        if now - st.get("esum_ts", 0) < 55:
            return
        st["esum_ts"] = now
        try:
            from ..collectors.espn import live_summary, merge_team, merge_xg
            es = live_summary(m)
        except Exception as e:
            db.log_job("live", "error", f"ESPN stats {m['id']}: {e}")
            return
        if not es:
            return
        st["esum"], st["esum_ok"] = es, now
        st["mom"] = es.get("mom")
        st["stats"] = merge_team(st.get("stats_sofa"), es, st.get("sofa_t"))
        xg = merge_xg(st.get("xg_sofa"), es, st.get("sofa_t"))
        if xg[0] is not None:
            st["xg"] = xg
            for i, sd in enumerate(("H", "A")):
                if xg[i] is not None:
                    st["stats"][sd]["xg"] = xg[i]

    def statshub_live_odds(self, m: dict) -> int:
        """Cuotas en directo de varias casas (Pinnacle, Betfair, Kambi, Paddy Power…) vía Statshub: solo las que se han
        actualizado en los últimos 10 minutos. bet365 no publica en directo ahí. Se guardan solo las últimas (sin histórico)."""
        from datetime import datetime
        data = shub().odds_comparison(m["id"], m["home_name"], m["away_name"])
        upd = data.get("bookmakerUpdatedAt") or {}
        now = time.time()
        fresh = set()
        for b, t in upd.items():
            try:
                if now - datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() < 600:
                    fresh.add(b)
            except Exception:
                pass
        rows = [r for r in sh.parse_odds_comparison(data, m["home_name"], m["away_name"]) if r["bookmaker"] in fresh]
        db.kv_set(f"live_book_odds:{m['id']}", {"ts": int(now), "books": sorted(fresh),
                                                 "rows": [[r["bookmaker"], r["market"], r["selection"], r["line"], r.get("period", "FT"), r["odds"]]
                                                          for r in rows if r.get("period", "FT") == "FT"]})
        return len(rows)

    def sofa_state(self, m: dict, st: dict, now: float) -> dict | None:
        """Estado real del partido según Sofascore (Statshub a veces se queda en «2nd half» cuando ya ha acabado).
        ESPN primero (gratis y no bloquea): así Sofascore se reserva para lo que solo da él (estadísticas, onces)."""
        if True:
            if now - st.get("es_ts", 0) < 60 and st.get("es"):
                ss = dict(st["es"])
                if ss["type"] == "inprogress" and ss.get("minute") is not None and not ss.get("ht"):
                    ss["minute"] = ss["minute"] + (now - st["es_ts"]) / 60
                return ss
            try:
                from ..collectors.espn import live_state
                ss = live_state(m)
            except Exception:
                ss = None
            if ss:
                prev = st.get("es")
                if prev and ((prev.get("hg"), prev.get("ag"), tuple(prev.get("reds") or (0, 0))) !=
                             (ss.get("hg"), ss.get("ag"), tuple(ss.get("reds") or (0, 0)))):
                    # gol o roja: refrescar ya Sofascore (xG, once, cambios), sin esperar a su turno
                    if now - self.last_sofa.get(m["id"], 0) > 30:
                        self.last_sofa[m["id"]] = 0
                        st["lu_ts"] = 0
                    # C5: tras un gol o una roja, cuotas y proyecciones al momento (bet365 reabre con precios genéricos)
                    st["props_ts"] = 0
                    st["odp_ts"] = 0
                    st["fm_ts"] = 0
                    st["event_at"] = now
                st["es"], st["es_ts"] = ss, now
                if ss.get("reds") and sum(ss["reds"]) >= sum(st.get("reds") or (0, 0)):
                    st["reds"] = tuple(ss["reds"])      # rojas al momento (Sofascore solo se mira cada 4 min)
                return ss
        if not sofa().ok:
            if now - st.get("es_ts", 0) < 60 and st.get("es"):
                ss = dict(st["es"])
                if ss["type"] == "inprogress" and ss.get("minute") is not None and not ss.get("ht"):
                    ss["minute"] = ss["minute"] + (now - st["es_ts"]) / 60
                return ss
            try:
                from ..collectors.espn import live_state
                ss = live_state(m)
            except Exception:
                ss = None
            if ss:
                st["es"], st["es_ts"] = ss, now
            return ss
        if now - st.get("ss_ts", 0) < 60 and st.get("ss"):
            ss = dict(st["ss"])
            if ss["type"] == "inprogress" and ss.get("minute") is not None and not ss.get("ht"):
                ss["minute"] = ss["minute"] + (now - st["ss_ts"]) / 60
            return ss
        try:
            ev = sofa().event(m["id"])
        except Exception:
            ev = {}
        if not ev:
            return st.get("ss")
        stt = ev.get("status") or {}
        desc = str(stt.get("description") or "").lower()
        ss = {"type": stt.get("type"), "desc": desc, "hg": (ev.get("homeScore") or {}).get("current"),
              "ag": (ev.get("awayScore") or {}).get("current"), "ht": "halftime" in desc or desc == "ht"}
        ss["minute"] = sf.minute_of(ev, now) if ss["type"] == "inprogress" else None
        st["ss"], st["ss_ts"] = ss, now
        return ss

    def fallback_minute(self, m: dict, st: dict, now: float) -> float | None:
        """Statshub no siempre da el inicio del periodo: minuto de Sofascore (cada 60 s) o, si no, por el reloj y el estado."""
        txt = str(st.get("status_txt") or "").lower()
        if not txt or txt.strip() == "ft" or any(k in txt for k in ("ended", "finished", "not started", "notstarted", "postponed", "canceled")):
            return None
        if "half" in txt and "time" in txt:
            return 45.0
        if sofa().ok and now - st.get("sofa_ev_ts", 0) > 60:
            st["sofa_ev_ts"] = now
            try:
                ev = sofa().event(m["id"])
                mm = sf.minute_of(ev, now)
                if mm is not None:
                    st["sofa_min"] = (mm, now)
            except Exception:
                pass
        if st.get("sofa_min"):
            mm, t0 = st["sofa_min"]
            if "half" in txt and "time" in txt:
                return 45.0
            return mm + (now - t0) / 60
        el = (now - m["kickoff"]) / 60
        if "1st" in txt:
            return min(max(el, 0), 47)
        if "half" in txt:
            return 45.0
        if "2nd" in txt:
            return min(max(45 + el - 62, 46), 98)
        return None

    def props(self, m: dict, minute: float, st: dict, now: float, fair: dict | None = None) -> dict | None:
        """Proyecciones de equipo y jugadores (cada ~60 s) y avisos con base estadística."""
        from ..pricing import count_models
        from . import props as lp
        if not sofa().ok and not (st.get("esum") or st.get("fm") or st.get("lu_raw")):
            return st.get("props")          # ni Sofascore ni FotMob/ESPN: no hay con qué calcular
        if now - st.get("props_ts", 0) < (60 if now - max(st.get("esum_ok", 0), st.get("fm_ok", 0)) < 120 else 150) and st.get("props"):
            return st["props"]
        st["props_ts"] = now
        if "cm" not in st:
            st["cm"] = count_models(m)
            try:
                st["anchor"] = lp.market_anchor(m["id"])
            except Exception:
                st["anchor"] = {}
        lu_every = max(600.0, self.sofa_every) if self.fm_fresh(st, now) else self.sofa_every
        if sofa().ok and (now - st.get("lu_ts", 0) > lu_every or not st.get("lu_raw")):
            new = sofa().lineups(m["id"])
            if new:
                st["lu_raw"], st["lu_ok"] = new, now
            st["lu_ts"] = now
        if not st.get("lu_raw") or st["lu_raw"].get("_src") == "db":
            # Sofascore en pausa (o recién reiniciado): el once guardado antes del partido, con stats de FotMob/ESPN
            if now - st.get("lu_db_ts", 0) > 300:
                st["lu_db_ts"] = now
                st["lu_raw"] = lineup_from_db(m["id"]) or st.get("lu_raw")
        lu_raw = st.get("lu_raw")
        if lu_raw and lu_raw.get("_src") == "db":
            lu_raw = _set_minutes(lu_raw, minute, st.get("fm") if self.fm_fresh(st, now) else None)
        if lu_raw and self.fm_fresh(st, now) and st.get("fm"):
            try:
                from ..collectors.fotmob import overlay_lineup as fm_overlay
                lu_raw = fm_overlay(lu_raw, st["fm"])        # stats por jugador de FotMob (Opta, ≈25 s)
            except Exception as e:
                db.log_job("live", "error", f"FotMob jugadores {m['id']}: {e}")
        elif lu_raw and st.get("esum"):
            try:
                from ..collectors.espn import overlay_lineup
                lu_raw = overlay_lineup(lu_raw, st["esum"], st.get("lu_ok"))      # stats por jugador al minuto
            except Exception as e:
                db.log_job("live", "error", f"ESPN jugadores {m['id']}: {e}")
        incs_raw = st.get("incs_raw") or []
        if "prerates" not in st and lu_raw:
            ids = [((p.get("player") or {}).get("id")) for side in ("home", "away") for p in (lu_raw.get(side) or {}).get("players", [])]
            st["prerates"] = lp.pre_player_rates([i for i in ids if i], m["kickoff"])
        snap = lp.build(m, minute, st, st["cm"], lu_raw, incs_raw, st.get("prerates") or {})
        # mapa de tiros (posición, xG, cabeza/pie, resultado), zonas de ataque y presión minuto a minuto
        src = st.get("fm") if self.fm_fresh(st, now) else st.get("esum")
        if src:
            fmsrc = src is st.get("fm")
            snap["shots"] = [{**{k: x.get(k) for k in ("minute", "side", "player", "res", "body", "zone", "xg")},
                              # todo a 0-100 atacando hacia la derecha (FotMob da metros 105×68; ESPN ya va 0-100)
                              "x": round(x["x"] * 100 / 105 if fmsrc else x["x"], 1),
                              "y": round(x["y"] * 100 / 68 if fmsrc else x["y"], 1)}
                             for x in src.get("shots") or [] if x.get("x") is not None and x.get("y") is not None][-60:]
            snap["shots_src"] = "FotMob (Opta)" if src is st.get("fm") else "ESPN (xG propio)"
            snap["zones"] = src.get("zones")
            snap["momentum"] = (src.get("momentum") or [])[-100:]
        try:
            from ..collectors.s365 import net_time
            snap["net_time"] = net_time(m)              # tiempo neto con balón en juego (365Scores)
            if snap["net_time"]:
                st["net_ok"] = now
                st["net"] = snap["net_time"]
        except Exception:
            pass
        sig = lp.signals(snap, m, lp.live_odds(m["id"], int(now) - 240), fair)
        snap["signals"] = sig
        db.kv_set(f"live_props:{m['id']}", snap)
        # solo se avisa con datos recientes (ESPN o Sofascore de hace menos de 2 min): nada de avisos con números viejos
        fresh = now - max(st.get("fm_ok", 0), st.get("esum_ok", 0), st.get("lu_ok", 0), self.last_sofa.get(m["id"], 0)) < 120
        snap["fresh"] = fresh
        for s_ in sig:
            if not fresh:
                break
            if (s_["ev"] or 0) > 0.08 or (s_["kind"] == "player" and s_["p"] >= 0.6):
                alert("live_prop", f"{m['home_name']} – {m['away_name']} ({int(minute)}'): {s_['title']}",
                      f"{s_['why']}. Sale el {round(s_['p'] * 100)} %: entra si la cuota es {s_['min_odds']:.2f} o más"
                      + (f" (ahora {s_['odds']:.2f})" if s_["odds"] else ""), match_id=m["id"],
                      payload={**{k: s_.get(k) for k in ("kind", "title", "market", "sel", "line", "pid", "stat", "p", "odds", "min_odds", "est_odds")},
                               "minute": round(minute), "score": list(st.get("score") or (0, 0)),
                               "stake_eur": _lp_stake(s_)},
                      dedupe=f"lp:{m['id']}:{s_['title']}", push=True)
        st["props"] = {"signals": sig[:8], "ts": snap["ts"]}
        return st["props"]

    def evaluate(self, m: dict, minute: float, st: dict) -> dict | None:
        pred = db.q1("SELECT payload FROM predictions WHERE match_id=? AND model='prematch'", (m["id"],))
        if not pred:
            return {"match_id": m["id"], "minute": minute, "note": "sin predicción pre-partido"}
        pay = json.loads(pred["payload"])
        lh, la = pay["lh"], pay["la"]
        # mejor punto de partida: los goles esperados que salen de las cuotas de todas las casas antes del partido
        if "lam_mk" not in st:
            try:
                from ..pricing import price_match
                lam = (price_match(m["id"]) or {}).get("lambdas") or {}
                st["lam_mk"] = (lam["lh"], lam["la"]) if lam.get("lh") else None
            except Exception:
                st["lam_mk"] = None
        if st.get("lam_mk"):
            lh, la = st["lam_mk"]
        hg, ag = st.get("score", (m.get("hg") or 0, m.get("ag") or 0))
        rh, ra = st.get("reds", (0, 0))
        xh, xa = st.get("xg", (None, None))
        added = st.get("added")
        nt = st.get("net") or {}
        if added is None and minute >= 70 and nt.get("ratio"):
            # C5: sin añadido anunciado, se estima con el tiempo neto: partido muy cortado → más añadido
            added = float(min(9.0, max(2.0, 3.5 + (0.60 - nt["ratio"]) * 25)))
        ls = LiveState(minute, hg, ag, rh, ra, xh, xa, added)
        fair = self.model.fair_prices(lh, la, ls)
        fair.pop("matrix", None)
        snap = {"match_id": m["id"], "match": f"{m['home_name']} - {m['away_name']}", "comp": m["comp"],
                "minute": round(minute, 1), "score": f"{hg}-{ag}", "reds": [rh, ra], "xg": [xh, xa],
                "fair": fair, "signals": []}
        from ..backtest.inplay import MatchTL, _context_ok, _make_sel, _state_at, _trigger_ok, _role_side
        fav = "H" if pay["pH"] >= pay["pA"] else "A"
        evs = sorted(st.get("events", []), key=lambda e: e["minute"])
        if xh is not None:
            evs = evs + [{"minute": max(0.0, minute - 0.5), "kind": "shot", "side": "H", "xg": xh},
                         {"minute": max(0.0, minute - 0.5), "kind": "shot", "side": "A", "xg": xa or 0}]
        tl = MatchTL(m["id"], m["comp"], m["season"] or "", m["kickoff"], lh, la, fav, max(pay["pH"], pay["pA"]),
                     hg, ag, evs, pay.get("feats", {}))
        s_now = _state_at(tl, minute + 0.01)
        s_now.update({"hg": hg, "ag": ag, "rh": rh, "ra": ra})
        for strat in watch_strategies():
            lo, hi = strat.trigger.get("minute", [1, 90])
            if not (lo <= minute <= hi + 1) or not _context_ok(strat, tl) or not _trigger_ok(strat, tl, s_now, minute):
                continue
            sel = _make_sel(strat.market, tl, s_now)
            if sel is None:
                continue
            P = self.model.final_distribution(lh, la, ls)
            w, p, l = selection_probs(sel, P)
            q = w / max(w + l, 1e-9)
            edge_req = max(0.03, strat.model_filter.get("min_edge") or 0.0)
            min_odds = round((1 + edge_req) / q, 2) if q > 0 else None
            stake = round(max(1.0, settings.bankroll * 0.01), 2)
            live_o = live_price(m["id"], sel)
            enter = live_o is None or (min_odds is not None and live_o >= min_odds)
            hedge_txt = ""
            if strat.exit.get("type") == "hedge":
                hedge_txt = (f"\nSi {('empatan' if strat.exit.get('on') == 'equalizer' else 'hay gol')}: cubrir "
                             f"{strat.exit['hedge']['market']} {strat.exit['hedge'].get('line', '')} del que iba ganando. "
                             f"Stake = {'stake/(cuota−1)' if strat.exit.get('size') == 'zero' else 'stake×cuota_entrada/cuota_cobertura'}"
                             f" (te aviso con la cifra exacta).")
            sig = {"strategy": strat.name, "strategy_id": strat.id, "selection": sel.label(m["home_name"], m["away_name"]),
                   "fair_odds": round(1 / q, 2) if q else None, "min_odds": min_odds, "stake": stake, "status": strat.description,
                   "live_odds": live_o, "enter": enter, "market": sel.market, "sel": sel.selection, "line": sel.line}
            snap["signals"].append(sig)
            if not enter:      # hay cuota real y no llega al mínimo: se muestra, pero ni se avisa ni se apuesta
                continue
            grp = f"live:{strat.id}:{m['id']}"
            _paper_bet(strat, m["id"], sel, live_o or min_odds or 0, stake, q, minute, group=grp,
                       mode="paper" if strat.description in ("paper", "live", "validated") else "watch")
            db.execute("UPDATE bets SET notes=? WHERE group_id=? AND notes IS NULL",
                       (json.dumps({"leader": "H" if hg > ag else ("A" if ag > hg else None), "hedge": strat.exit}), grp))
            alert("live", f"⚡ {m['home_name']} {hg}-{ag} {m['away_name']} ({int(minute)}')",
                  f"{strat.name}\nApostar: {sig['selection']}\nEntrar solo si bet365 paga ≥ {min_odds} (justa {sig['fair_odds']})"
                  + (f"\nAhora mismo se paga {live_o}" if live_o else "") + "\n"
                  f"Stake: {stake} €{hedge_txt}", match_id=m["id"], strategy_id=strat.id, payload=sig, dedupe=grp,
                  push=strat.description in ("paper", "live", "validated"))
        self._hedge_checks(m, ls, lh, la, snap)
        return snap

    def _hedge_checks(self, m: dict, ls: LiveState, lh: float, la: float, snap: dict) -> None:
        """Para apuestas en vivo abiertas con regla de cobertura: si se cumple el evento, calcula la cobertura."""
        rows = db.q("SELECT * FROM bets WHERE match_id=? AND status='open' AND group_id LIKE 'live:%' AND notes IS NOT NULL", (m["id"],))
        for b in rows:
            info = json.loads(b["notes"])
            ex = info.get("hedge") or {}
            if ex.get("type") != "hedge" or info.get("hedged"):
                continue
            on = ex.get("on")
            if on == "equalizer" and ls.hg != ls.ag:
                continue
            if on == "goal" and (ls.hg, ls.ag) == (b["base_h"], b["base_a"]):
                continue
            side = info.get("leader") if ex["hedge"].get("selection") == "original_leader" else ex["hedge"].get("selection")
            if side not in ("H", "A"):
                continue
            hsel = Selection("AH", side, float(ex["hedge"].get("line", 0.0)), base_h=ls.hg, base_a=ls.ag)
            P = self.model.final_distribution(lh, la, ls)
            w, p, l = selection_probs(hsel, P)
            fair_h = round(1 + l / max(w, 1e-9), 2)
            size = ex.get("size", "zero")
            def stake_for(o):
                return hedge_to_zero(b["stake"], o) if size == "zero" else hedge_equalize(b["stake"], b["odds"], o)
            table = {f"{o:.2f}": round(stake_for(o), 2) for o in np.round(np.linspace(max(1.2, fair_h * 0.85), fair_h * 1.15, 6), 2)}
            real_h = live_price(m["id"], hsel)
            msg = (f"Cobertura {hsel.label(m['home_name'], m['away_name'])}\nCuota justa ≈ {fair_h}"
                   + (f" · ahora se paga {real_h} → stake {round(stake_for(real_h), 2)} €" if real_h else "")
                   + ". Stake según la cuota que veas en bet365:\n"
                   + "\n".join(f"  @{k} → {v} €" for k, v in table.items()))
            info["hedged"] = True
            db.execute("UPDATE bets SET notes=? WHERE id=?", (json.dumps(info), b["id"]))
            # pierna de cobertura en papel a la cuota justa
            db.execute("INSERT INTO bets(strategy_id,group_id,match_id,created,mode,market,selection,line,period,base_h,base_a,odds,"
                       "odds_source,fair_prob,stake,minute,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
                       (b["strategy_id"], b["group_id"], m["id"], int(time.time()), b["mode"], "AH", side, hsel.line, "FT",
                        ls.hg, ls.ag, real_h or fair_h, "sofascore_live" if real_h else "model_live", w / max(w + l, 1e-9),
                        round(stake_for(real_h or fair_h), 2), ls.minute))
            active = db.q1("SELECT 1 FROM strategies WHERE id=? AND status IN ('paper','live','validated')", (b["strategy_id"],))
            alert("hedge", f"🛡️ Cubrir ahora: {m['home_name']} {ls.hg}-{ls.ag} {m['away_name']} ({int(ls.minute)}')", msg,
                  match_id=m["id"], strategy_id=b["strategy_id"], payload={"table": table, "fair": fair_h},
                  dedupe=f"hedge:{b['id']}", push=bool(active))
            snap["signals"].append({"strategy": "cobertura", "selection": hsel.label(m["home_name"], m["away_name"]),
                                    "fair_odds": fair_h, "live_odds": real_h, "stake_table": table})


# ------------------------------------------------------------------ jugadores y alineaciones
def _lp_stake(s_: dict) -> float:
    from ..followup import page_stake
    return page_stake(s_.get("p"), s_.get("odds") or s_.get("min_odds"), s_.get("kind") == "player")


def void_match_bets(mid: int, why: str = "postponed") -> int:
    """Partido aplazado/suspendido: todas sus apuestas, anuladas (se devuelve el stake; no cuentan ni para
    rentabilidad ni para aprender)."""
    k = db.q1("SELECT COUNT(*) AS n FROM bets WHERE match_id=? AND status!='void'", (mid,))["n"]
    db.execute("UPDATE bets SET status='void', pnl=0, settled=? WHERE match_id=? AND status!='void'", (int(time.time()), mid))
    db.execute("DELETE FROM timeline WHERE match_id=? AND source='espn'", (mid,))
    try:
        db.execute("DELETE FROM reports WHERE match_id=?", (mid,))
    except Exception:
        pass
    if k:
        db.log_job("results", "info", f"partido {mid} {why}: {k} apuestas anuladas")
    return k or 0


def fix_postponed(hours: float = 96) -> int:
    """Repara partidos dados por terminados que en realidad se aplazaron o suspendieron (ESPN los marca como
    acabados): los pasa a aplazado/cancelado y anula sus apuestas."""
    from ..collectors.espn import cancel_status, find_event
    now = int(time.time())
    n = 0
    for m in db.q("SELECT * FROM matches WHERE status IN ('finished','inprogress','notstarted') AND kickoff BETWEEN ? AND ?",
                  (now - int(hours * 3600), now - 1800)):
        try:
            e = find_event(m)
        except Exception:
            e = None
        bad = cancel_status(e) if e else None
        if bad:
            db.execute("UPDATE matches SET status=?, hg=NULL, ag=NULL WHERE id=?", (bad, m["id"]))
            void_match_bets(m["id"], bad)
            n += 1
    return n


def lineup_from_db(mid: int) -> dict | None:
    """El once guardado antes del partido (tabla lineups) con la forma de la alineación de Sofascore."""
    rows = db.q("SELECT side, player_id, name, position, jersey, starter FROM lineups WHERE match_id=?", (mid,))
    if not rows or not any(r["starter"] for r in rows):
        return None
    out = {"_src": "db", "home": {"players": []}, "away": {"players": []}}
    for r in rows:
        out["home" if r["side"] == "H" else "away"]["players"].append(
            {"player": {"id": r["player_id"], "name": r["name"], "shortName": r["name"]}, "jerseyNumber": r["jersey"],
             "position": (r["position"] or "M")[:1], "substitute": not r["starter"], "statistics": {}})
    return out


def _set_minutes(lu: dict, minute: float, fm: dict | None) -> dict:
    """Minutos jugados de cada uno (titulares: el minuto actual; los que entraron: desde su cambio, según FotMob)."""
    import copy
    from ..collectors.espn import _sim
    out = copy.deepcopy(lu)
    for sd, key in (("H", "home"), ("A", "away")):
        fl = ((fm or {}).get("P") or {}).get(sd, [])
        for p in (out.get(key) or {}).get("players") or []:
            nm = (p.get("player") or {}).get("name") or ""
            hit = max(fl, key=lambda e: _sim(nm, e["name"]), default=None)
            hit = hit if hit and _sim(nm, hit["name"]) >= 0.8 else None
            st = p.setdefault("statistics", {})
            if not p.get("substitute"):
                st["minutesPlayed"] = round(minute)
            elif hit and hit.get("sub_in"):
                st["minutesPlayed"] = max(1, round(minute - (hit.get("sub_min") or minute)))
    return out


def backfill_players(limit: int = 200, log=print) -> int:
    """Alineaciones jugadas + stats por jugador de todos los equipos seguidos."""
    from ..collectors import players as pl
    teams = db.q("SELECT DISTINCT home_id AS id FROM matches WHERE home_id>0 UNION SELECT DISTINCT away_id FROM matches WHERE away_id>0")
    n = 0
    for t in teams:
        try:
            rows = shub().team_last_games(t["id"], 1, limit, ttl=86400)
            n += pl.store_last_games(pl.parse_last_games(rows))
        except Exception as e:
            log(f"  jugadores equipo {t['id']}: {e}")
    return n


def refresh_lineups(hours_ahead: float = 30) -> int:
    """Alineaciones previstas/confirmadas de los próximos partidos (más a menudo cerca del saque)."""
    from ..collectors import players as pl
    now = int(time.time())
    ms = db.q("SELECT id, kickoff, lineup_status FROM matches WHERE status='notstarted' AND kickoff BETWEEN ? AND ?",
              (now, now + int(hours_ahead * 3600)))
    if not ms:
        return 0
    try:
        status = shub().lineup_status([m["id"] for m in ms])
    except Exception:
        status = {}
    n = 0
    for m in ms:
        st = status.get(m["id"], m["lineup_status"])
        have = db.q1("SELECT source, MAX(ts) AS ts FROM lineups WHERE match_id=? AND source IN ('predicted','confirmed')", (m["id"],))
        left = (m["kickoff"] - now) / 3600
        stale = not have or not have["ts"] or now - have["ts"] > (3600 * 3 if left > 3 else 900)
        if st in ("predicted", "confirmed") and (stale or (st == "confirmed" and (have or {}).get("source") != "confirmed")):
            try:
                data = shub().predicted_lineup(m["id"])
                try:
                    forms = shub().formations(m["id"])
                except Exception:
                    forms = {}    # Statshub da 404 hasta que hay formaciones: el once vale igual
                n += pl.store_predicted(m["id"], data, forms)
                db.execute("UPDATE matches SET lineup_status=? WHERE id=?", (st, m["id"]))
                db.execute("DELETE FROM reports WHERE match_id=?", (m["id"],))  # el informe se rehace con el once nuevo
            except Exception as e:
                db.log_job("lineups", "error", f"{m['id']}: {e}")
        # bajas reales (lesión, sanción, duda) que publica Sofascore con la alineación
        mk = db.kv_get(f"missing:{m['id']}") or {}
        busy = db.q1("SELECT COUNT(*) AS n FROM matches WHERE status='inprogress'")["n"] > 0
        if sofa().ok and left < 48 and (not busy or left < 2) and now - (mk.get("ts") or 0) > (3 * 3600 if left > 6 else 1200):
            try:
                new = parse_missing(sofa().lineups(m["id"], ttl=600))
                ids = {p.get("id") for p in new}
                keep = [p for p in (mk.get("players") or []) if p.get("src") == "fotmob" and p.get("id") not in ids]   # las de FotMob se quedan
                db.kv_set(f"missing:{m['id']}", {**mk, "ts": int(now), "players": new + keep})
            except Exception as e:
                db.log_job("lineups", "error", f"bajas {m['id']}: {e}")
    return n


def parse_missing(lu: dict) -> list[dict]:
    out = []
    for side, key in (("H", "home"), ("A", "away")):
        for p in (lu.get(key) or {}).get("missingPlayers") or []:
            pl = p.get("player") or {}
            desc = str(p.get("description") or p.get("reason") or "")
            kind = p.get("type") or "missing"
            reason = ("sancionado" if "susp" in desc.lower() or "card" in desc.lower() else "lesionado" if desc and desc != "0" else "baja")
            out.append({"side": side, "id": pl.get("id"), "name": pl.get("name") or pl.get("shortName"), "pos": pl.get("position"),
                        "status": "duda" if kind == "doubtful" else "baja", "reason": reason, "detail": desc})
    return out
